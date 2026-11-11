"""Background, model-scoped Bailian trial inventory and admission."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .bailian_quota import CLIQuotaSource, ModelAccessSource, VerifiedIdentity, beijing_today, parse_observation
from .common import Cancelled, ToolError, atomic_json
from .mt_admission import ModelEvaluator, admission_key
from .mt_identity import canonical_model
from .mt_backend import key_fingerprint, normalize_mt_url
from .mt_control import MTControl

TRIAL_PROVIDER = 'transcribe-bailian-trial'
MANAGED_PREFIX = TRIAL_PROVIDER + '-'
ROLES = ('translate', 'review', 'judge')


def provider_id(model):
    return MANAGED_PREFIX + hashlib.sha256(model.encode()).hexdigest()[:12]


class _BackgroundCancel:
    def __init__(self, parent, stop):
        self.parent, self.stop = parent, stop

    def is_set(self):
        return self.stop.is_set() or self.parent.stopping


class BailianFreeGuard:
    """One worker per pipeline; never wait for cloud discovery on a stage thread."""
    def __init__(self, config, control, *, source=None, access=None, evaluator=None,
                 job_id='', clock=time.monotonic):
        self.config, self.parent, self.clock, self.job_id = config, control, clock, job_id
        self.stop = threading.Event()
        self.control = MTControl(_BackgroundCancel(control, self.stop), drain_seconds=0)
        self.source, self.access = source or CLIQuotaSource(), access or ModelAccessSource()
        self.evaluator = evaluator
        self.lock = threading.RLock()
        self.route_lock = threading.Lock()
        self.thread = None
        self.discovery = None
        self.rows, self.allowed, self.exhausted = {}, set(), set()
        self.offsets = {}
        self.state_path = config.data / 'bailian-trial-state.json'
        self.state = self._load_state()
        self.exhausted.update(m for m, row in self.state['models'].items()
                              if row.get('stopped_job') == job_id and row.get('status') in
                              {'quota_exhausted', 'account_credit', 'permission_denied', 'user_paused'})
        self.next_refresh = 0
        self.failures = 0

    def _load_state(self):
        if self.state_path.exists():
            try:
                value = json.loads(self.state_path.read_bytes())
            except (OSError, ValueError):
                raise ToolError('百炼试用状态无效，请检查保存记录。', 'mt_config') from None
            if (not isinstance(value, dict) or value.get('version') != 1 or
                    not isinstance(value.get('models'), dict) or not isinstance(value.get('evaluations'), dict) or
                    type(value.get('paused')) is not bool or not isinstance(value.get('excluded'), list) or
                    any(not isinstance(m, str) for m in value['excluded']) or
                    any(not isinstance(r, dict) or not isinstance(m, str) for m, r in value['models'].items())):
                raise ToolError('百炼试用状态无效，请检查保存记录。', 'mt_config')
            return value
        return {'version': 1, 'paused': False, 'excluded': [], 'models': {}, 'evaluations': {}}

    def _save(self):
        with self.lock:
            self.control.check_start()
            if self.state_path.exists():
                latest = self._load_state()
                self.state['paused'] = latest.get('paused', False)
                self.state['excluded'] = latest.get('excluded', [])
            atomic_json(self.state_path, self.state)

    def _audit(self, reason, model='', before=None, after=None, *, actor='transcribe.trial-manager'):
        path = self.config.root / '.build' / 'mt-diagnostics' / 'trial-management.jsonl'
        record = {'time': time.time(), 'actor': actor, 'job_id': self.job_id,
                  'reason': reason, 'model': model, 'before': before, 'after': after}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            self.state['audit_status'] = 'write_failed'

    def _registry(self):
        return json.loads((Path(self.config.settings.magpie_data_dir) / 'providers.json').read_bytes())

    def _identity(self, provider):
        record = json.loads((self.config.data / 'bailian-quota-binding.json').read_bytes())
        if (record.get('invalidated') is not False or record.get('source') != 'user_confirmed' or
                type(record.get('version')) is not int or record['version'] not in (1, 2) or
                record.get('region') != 'cn-beijing' or record.get('workspace') != self.config.settings.workspace_id or
                provider.get('keys') or provider.get('headers') or provider.get('fallback') or
                normalize_mt_url(provider.get('chat')) !=
                f"https://{record['workspace']}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" or
                key_fingerprint(provider.get('key', '')) != record.get('key_fingerprint') or
                key_fingerprint(self.config.get_key()) != record.get('key_fingerprint') or
                not isinstance(record.get('cli_fingerprint'), str) or
                re.fullmatch(r'[0-9a-f]{64}', record['cli_fingerprint']) is None):
            raise ValueError('identity')
        proof = VerifiedIdentity(record['workspace'], record['key_fingerprint'], record['region'],
                                 record['cli_fingerprint'], record['version'])
        if not self.source.bound_to(proof):
            raise ValueError('identity')
        return proof

    def _command(self, *args):
        self.control.check_start()
        folder = Path(self.config.settings.magpie_data_dir)
        executable = folder.parent / 'magpie.com'
        if folder.name != 'data' or not (folder.parent / '.portable').is_file() or not executable.is_file():
            raise ToolError('无法管理百炼试用入口；请检查 magpie 便携目录。', 'magpie_group')
        process = None
        try:
            deadline = time.monotonic() + (30 if args[:2] == ('provider', 'refresh') else 5)
            process = subprocess.Popen([str(executable), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while process.poll() is None:
                self.control.check_inflight(drain=False)
                if time.monotonic() >= deadline:
                    raise ValueError()
                self.stop.wait(.05)
            if process.returncode:
                raise ValueError()
            self.control.check_start()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ToolError('无法更新百炼试用入口；商业后备仍可用。', 'magpie_group') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def prepare(self, settings, base_url):
        from .magpie import bound_gateway
        self.parent.check_start()
        if not bound_gateway(settings, base_url):
            return
        effective = self.config.mt_settings()
        expected = {'mt_model': 'group/transcribe', 'mt_fallback_model': 'group/transcribe-paid',
                    'mt_review_model': 'group/transcribe-review', 'mt_review_fallback_model': 'group/transcribe-review-paid',
                    'mt_judge_model': 'group/transcribe-judge', 'mt_judge_fallback_model': 'group/transcribe-judge-paid'}
        if any((getattr(effective, field) or expected[field]) != value for field, value in expected.items()):
            return  # Custom/remote role groups remain administrator-managed.
        with self.lock:
            raw = self._registry()
            template = next((p for p in raw['providers'] if p['id'] == TRIAL_PROVIDER), None)
            if template is None:
                self.exhausted.update(self.state['models'])
                for model in list(self.state['models']):
                    self.remove(model, 'identity_changed')
                return
            try:
                self._identity(template)  # Local credentials only; no quota network request.
            except (OSError, ValueError, KeyError, TypeError, ToolError):
                self.exhausted.update(self.state['models'])
                for model in list(self.state['models']):
                    self.remove(model, 'identity_changed')
                return
            providers = {p['id']: p for p in raw['providers']}
            for model, item in list(self.state['models'].items()):
                lane = providers.get(item['provider'])
                if lane and (lane.get('key') != template['key'] or lane.get('chat') != template['chat'] or
                             lane.get('keys') or lane.get('fallback') or lane.get('headers') and
                             lane['headers'] != {'X-Transcribe-Trial-Model': model}):
                    self.exhausted.add(model)
                    self.remove(model, 'identity_changed')
            if self.thread is None:
                for path in (Path(settings.magpie_data_dir) / 'routing').glob('*.jsonl'):
                    self.offsets[str(path)] = path.stat().st_size
                self.thread = threading.Thread(target=self._run, name='bailian-trial', daemon=True)
                self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        if self.discovery is not None:
            self.discovery.join()

    def _run(self):
        while not self.control.stopping:
            try:
                self.poll_routes()
                if self.clock() >= self.next_refresh and (self.discovery is None or not self.discovery.is_alive()):
                    self.discovery = threading.Thread(target=self._discover, name='bailian-admission', daemon=True)
                    self.discovery.start()
            except Cancelled:
                break
            except Exception:
                self.failures += 1
                self.next_refresh = self.clock() + min(300, 60 * 2 ** min(self.failures, 3))
                self._audit('refresh_failed')
            self.stop.wait(1)

    def _discover(self):
        try:
            self.refresh()
        except Cancelled:
            pass
        except Exception:
            self.failures += 1
            self.next_refresh = self.clock() + min(300, 60 * 2 ** min(self.failures, 3))
            self._audit('refresh_failed')

    def poll_routes(self):
        with self.route_lock:
            self._poll_routes()

    def _poll_routes(self):
        folder = Path(self.config.settings.magpie_data_dir) / 'routing'
        for path in sorted(folder.glob('*.jsonl')):
            position = self.offsets.get(str(path), 0)
            if path.stat().st_size < position:
                position = 0
            with path.open('rb') as stream:
                stream.seek(position)
                while line := stream.readline(4 * 1024 * 1024):
                    if not line.endswith(b'\n'):
                        break
                    self.offsets[str(path)] = stream.tell()
                    try:
                        self.observe_route(json.loads(line))
                    except (ValueError, TypeError):
                        continue

    def observe_route(self, route):
        with self.lock:
            self._observe_route(route)

    def _observe_route(self, route):
        for attempt in route.get('tries', []):
            owner, model = attempt.get('id', ''), attempt.get('model', '')
            if not owner.startswith(MANAGED_PREFIX) or model not in self.state['models']:
                continue
            error = str(attempt.get('error', '')).lower()
            if 'free quota exhausted' in error or 'free tier' in error and 'exhaust' in error:
                self.exhausted.add(model)
                self.remove(model, 'quota_exhausted')
                self.next_refresh = 0
            elif attempt.get('fail') == 'credit':
                self.exhausted.update(self.state['models'])
                for other in list(self.state['models']):
                    self.remove(other, 'account_credit')
            elif attempt.get('status') in (401, 403):
                self.exhausted.add(model)
                self.remove(model, 'permission_denied')

    def remove(self, model, reason):
        with self.lock:
            self._remove(model, reason)

    def _remove(self, model, reason):
        item = self.state['models'].get(model)
        if not item:
            return
        member = item['provider'] + '/' + model
        groups = self._registry().get('groups', [])
        for group in groups:
            if group['id'].startswith('transcribe') and member in group.get('members', []):
                if len(group['members']) > 1:
                    self._command('group', 'set', group['id'], 'models-=' + member)
        # magpie requires at least one stored member. The last disabled lane
        # may remain as a placeholder; it is never an eligible route.
        provider = next((p for p in self._registry()['providers'] if p['id'] == item['provider']), None)
        if provider is not None and not provider.get('off'):
            self._command('provider', 'off', item['provider'])
            item['disabled_by_manager'] = True
        before = item.get('status')
        item['status'] = reason
        item['stopped_job'] = self.job_id
        self.allowed.discard((item['provider'], model))
        self._save()
        if before != reason:
            self._audit(reason, model, before, reason)

    def _lane(self, template, model, limits):
        with self.lock:
            return self._lane_locked(template, model, limits)

    def _lane_locked(self, template, model, limits):
        if model in self.exhausted:
            raise ToolError('本任务已退出该试用模型。', 'mt_config')
        identity = provider_id(model)
        providers = {p['id']: p for p in self._registry()['providers']}
        ceiling = self.state.setdefault('aggregate_rpm', template.get('maxRPM') or 60)
        known_limits = [limits] + [item.get('limits', {}) for item in self.state['evaluations'].values()]
        for known in known_limits:
            if known.get('workspace_rpm'):
                ceiling = min(ceiling, known['workspace_rpm'])
            if known.get('workspace_tpm'):
                ceiling = min(ceiling, known['workspace_tpm'] // (32768 + 4096))
        # Reserve four shares even while a replacement is being evaluated.
        if ceiling < 4 or limits.get('tpm') and limits['tpm'] < 32768 + 4096:
            raise ToolError('共享配额不足以安全拆分入口。', 'mt_config')
        if identity not in providers:
            # magpie deduplicates identical endpoint/key/header providers.
            # This non-secret model tag gives each lane its own cooldown key.
            self._command('provider', 'add', identity, 'id=' + identity, 'chat=' + template['chat'],
                          'key=' + template['key'], 'models=' + model, 'free=yes', 'list=off',
                          'header.X-Transcribe-Trial-Model=' + model)
            self._audit('provider_created', model, None, identity)
        else:
            lane = providers[identity]
            owned_off = self.state['models'].get(model, {}).get('disabled_by_manager')
            if lane.get('off') and not owned_off or lane.get('key') != template['key'] or lane.get('chat') != template['chat']:
                raise ToolError('试用模型入口已被人工修改。', 'mt_config')
            if lane.get('off') and owned_off:
                self._command('provider', 'on', identity)
                self.state['models'][model]['disabled_by_manager'] = False
        # Four shares: the existing commercial lane plus at most three trial
        # lanes (including the one under evaluation). Never clone the budget.
        rpm = ceiling // 4
        if limits.get('rpm'):
            rpm = min(rpm, limits['rpm'])
        if limits.get('tpm'):
            rpm = min(rpm, limits['tpm'] // (32768 + 4096))
        if providers.get(identity, {}).get('maxRPM'):
            rpm = min(rpm, providers[identity]['maxRPM'])
        for lane in providers.values():
            if lane['id'].startswith(MANAGED_PREFIX) and lane.get('maxRPM', 0) > ceiling // 4:
                self._command('provider', 'rpm', lane['id'], str(ceiling // 4))
        commercial = providers.get('transcribe-bailian')
        if commercial and commercial.get('chat') == template['chat'] and commercial.get('key') == template['key']:
            previous = commercial.get('maxRPM') or ceiling
            share = min(previous, max(1, ceiling // 4))
            if previous != share:
                self._command('provider', 'rpm', commercial['id'], str(share))
                self._audit('shared_budget', '', previous, share)
        self._command('provider', 'rpm', identity, str(rpm))
        return identity

    def _join(self, model, item):
        with self.lock:
            if model in self.exhausted:
                return
            self._join_locked(model, item)

    def _join_locked(self, model, item):
        latest = self._load_state()
        if latest['paused'] or model in latest['excluded']:
            self.remove(model, 'user_paused')
            return
        role = item['role']
        suffix = '' if role == 'translate' else '-' + role
        trial_group = 'transcribe-trial' + suffix
        free_group = 'transcribe-free' + suffix
        main = 'transcribe' + suffix
        edges = [(free_group, 'group/' + trial_group), (main, 'group/' + free_group)]
        if any(parent + ':' + child in self.state.get('excluded_groups', []) for parent, child in edges):
            self.remove(model, 'user_paused')
            return
        member = item['provider'] + '/' + model
        groups = {g['id']: g for g in self._registry().get('groups', [])}
        for parent, child in edges:
            group = groups.get(parent, {})
            if group.get('hidden') or child in group.get('off', []):
                exclusions = set(self.state.get('excluded_groups', []))
                exclusions.add(parent + ':' + child)
                self.state['excluded_groups'] = sorted(exclusions)
                self.remove(model, 'user_paused')
                return
        if trial_group not in groups:
            self._command('group', 'add', trial_group, 'models=' + member, 'routing=order', 'stays=off')
        elif member not in groups[trial_group].get('members', []):
            self._command('group', 'set', trial_group, 'models+=' + member)
        trial = 'group/' + trial_group
        if free_group not in groups:
            self._command('group', 'add', free_group, 'models=' + trial, 'routing=weight', 'stays=off')
        elif trial not in groups[free_group].get('members', []):
            self._command('group', 'set', free_group, 'models+=' + trial)
        paid = 'group/' + main + '-paid'
        if main in groups and 'group/' + free_group not in groups[main].get('members', []):
            # Incremental mutation in one CLI transaction moves only our
            # commercial fallback behind the newly available free subgroup.
            changes = (['models-=' + paid] if paid in groups[main].get('members', []) else [])
            self._command('group', 'set', main, *changes, 'models+=group/' + free_group + ',' + paid)
        before = item.get('status')
        item['status'] = 'active'
        item['joined'] = True
        owned_edges = self.state.setdefault('owned_edges', [])
        for parent, child in edges:
            if parent in groups or parent == free_group:
                edge = parent + ':' + child
                if edge not in owned_edges:
                    owned_edges.append(edge)
        self.allowed.add((item['provider'], model))
        self._save()
        if before != 'active':
            self._audit('model_admitted', model, before, 'active')

    def refresh(self):
        self.control.check_start()
        self.next_refresh = self.clock() + 60
        fresh = self._load_state()
        self.state['paused'], self.state['excluded'] = fresh.get('paused', False), fresh.get('excluded', [])
        if self.state['paused']:
            for model in list(self.state['models']):
                self.remove(model, 'user_paused')
            self.state['pool_status'] = 'user_paused'
            self._save()
            return
        raw = self._registry()
        groups = {g['id']: g for g in raw.get('groups', [])}
        exclusions = set(self.state.get('excluded_groups', []))
        owned_edges = self.state.setdefault('owned_edges', [])
        for item in self.state['models'].values():
            if item.get('joined'):
                suffix = '' if item['role'] == 'translate' else '-' + item['role']
                edges = ['transcribe-free' + suffix + ':group/transcribe-trial' + suffix]
                if 'transcribe' + suffix in groups:
                    edges.append('transcribe' + suffix + ':group/transcribe-free' + suffix)
                owned_edges.extend(edge for edge in edges if edge not in owned_edges)
        for edge in self.state.get('owned_edges', []):
            parent, child = edge.split(':', 1)
            group = groups.get(parent, {})
            if (child not in group.get('members', []) or child in group.get('off', []) or group.get('hidden')):
                exclusions.add(edge)
        self.state['excluded_groups'] = sorted(exclusions)
        template = next((p for p in raw['providers'] if p['id'] == TRIAL_PROVIDER), None)
        if template is None:
            return
        try:
            proof = self._identity(template)
        except (ValueError, KeyError, ToolError):
            for model in list(self.state['models']):
                self.remove(model, 'identity_changed')
            return
        # A process may have exited after provider creation and before the
        # local admission record committed. Only recover our exact tagged lane.
        for lane in raw['providers']:
            models = lane.get('models', [])
            if (len(models) == 1 and lane['id'] == provider_id(models[0]) and
                    models[0] not in self.state['models'] and lane.get('key') == template.get('key') and
                    lane.get('chat') == template.get('chat') and
                    lane.get('headers', {}).get('X-Transcribe-Trial-Model') == models[0]):
                evaluation = self.state['evaluations'].get(admission_key(template, models[0]))
                rejected = evaluation is not None and not evaluation.get('roles')
                self.state['models'][models[0]] = {'provider': lane['id'], 'role': 'translate', 'status': 'interrupted',
                                                  'disabled_by_manager': bool(rejected)}
                self.remove(models[0], 'admission_rejected' if rejected else 'interrupted')
        try:
            self.rows = parse_observation(self.source.query(control=self.control, timeout=15, proof=proof))
        except Cancelled:
            raise
        except Exception:
            self.state['quota_status'] = 'unknown'
            self._save()
            self.failures += 1
            self.next_refresh = self.clock() + min(300, 60 * 2 ** min(self.failures, 3))
            self._audit('quota_unknown')
            return
        self.failures = 0
        self.state['quota_status'] = 'known'
        today = beijing_today()
        providers = {p['id']: p for p in raw['providers']}
        groups = {g['id']: g for g in raw.get('groups', [])}
        for model, item in list(self.state['models'].items()):
            lane = providers.get(item['provider'])
            suffix = '' if item['role'] == 'translate' else '-' + item['role']
            group = groups.get('transcribe-trial' + suffix, {})
            member = item['provider'] + '/' + model
            manually_removed = (item.get('joined') and item.get('status') == 'active' and
                (member not in group.get('members', []) or member in group.get('off', []) or group.get('hidden')))
            manually_removed = manually_removed or any(edge in exclusions for edge in (
                'transcribe-free' + suffix + ':group/transcribe-trial' + suffix,
                'transcribe' + suffix + ':group/transcribe-free' + suffix))
            if (lane is None or lane.get('off') and not item.get('disabled_by_manager') or
                    model in self.state['excluded'] or manually_removed):
                item['manual_hold'] = True
                self.exhausted.add(model)
                self.remove(model, 'user_paused')
            elif model not in self.rows or not self.rows[model].usable(today):
                self.exhausted.add(model)
                self.remove(model, 'quota_exhausted' if model in self.rows and self.rows[model].remaining == 0 else 'quota_unavailable')
        active = [m for m, item in self.state['models'].items() if item.get('status') == 'active']
        if len(active) >= 3 and not any(self.state['models'][m]['role'] == 'translate' for m in active):
            self.remove(active.pop(), 'standby')
        candidates = sorted((m for m, row in self.rows.items() if row.usable(today) and
            m not in self.exhausted and m not in self.state['excluded'] and
            not self.state['models'].get(m, {}).get('manual_hold')),
            key=lambda m: (self.rows[m].expires, -self.rows[m].remaining, m))
        for model in candidates:
            self.control.check_start()
            if len(active) >= 3:
                break
            if model in active:
                continue
            key = admission_key(template, model)
            evaluation = self.state['evaluations'].get(key)
            if evaluation and evaluation.get('retry_after') and evaluation['retry_after'] <= time.time():
                evaluation = None
            if evaluation is None:
                self.state['pool_status'] = 'replenishing'
                self._save()
                try:
                    limits = self.access.inspect(self.config, model, self.control)
                    identity = self._lane(template, model, limits)
                except Cancelled:
                    raise
                except Exception:
                    self._audit('permission_or_limits_unknown', model)
                    continue
                item = self.state['models'].setdefault(model, {'provider': identity, 'role': 'translate'})
                item['status'] = 'evaluating'
                self._save()
                evaluator = self.evaluator or ModelEvaluator(self.config, self.control,
                    judge=self.state.get('admission_judge', 'transcribe-qiniu-paid/z-ai/glm-5.3-flash'))
                evaluation = evaluator.evaluate(identity + '/' + model)
                evaluation['limits'] = limits
                self.state['evaluations'][key] = evaluation
                self._save()
                self._audit('evaluation_finished', model, None, evaluation['roles'])
            if model in self.exhausted or self.state.get('paused') or model in self.state['excluded']:
                continue
            roles = evaluation.get('roles', [])
            if not roles:
                self.remove(model, 'admission_rejected')
                continue
            # Gateway maintenance uses this same lock when assigning roles.
            # Never publish an admission using reservations read before its switch.
            with self.lock:
                reserved = self.state.get('reserved_models', {})
                actual = evaluation.get('actual_models') or [canonical_model(model)]
                forced = {reserved[m] for m in actual if m in reserved}
                eligible = [r for r in ROLES if r in roles and (not forced or forced == {r})]
                if not eligible:
                    self.remove(model, 'role_conflict')
                    continue
                if (len(active) >= 2 and not any(self.state['models'][m]['role'] == 'translate' for m in active)
                        and 'translate' not in eligible):
                    self.remove(model, 'standby')
                    continue
                thinking = evaluation.get('non_thinking', {})
                role = min(eligible, key=lambda r: (sum(self.state['models'][m]['role'] == r for m in active),
                    r == 'review' and isinstance(thinking, dict) and not thinking.get('review')))
                identity = self._lane(template, model, evaluation.get('limits', {}))
                item = self.state['models'].setdefault(model, {'provider': identity, 'role': role, 'status': 'ready'})
                item['role'] = role
                for actual_model in actual:
                    reserved[actual_model] = role
                self.state['reserved_models'] = reserved
                self._join(model, item)
                if item.get('status') == 'active':
                    active.append(model)
        self.state['pool_status'] = 'ready' if active else 'no_candidates'
        self._rank_active(template, active)
        self._save()

    def _rank_active(self, template, active):
        """Order qualified lanes within each supplier without touching manual members."""
        with self.lock:
            for role in ROLES:
                names = [m for m in active if self.state['models'][m]['role'] == role and m not in self.exhausted]
                def rank(model):
                    result = self.state['evaluations'].get(admission_key(template, model), {})
                    measured = result.get('non_thinking', {})
                    quiet = isinstance(measured, dict) and measured.get(role, False)
                    return (role == 'review' and not quiet, self.rows[model].expires,
                            -self.rows[model].remaining, result.get('duration_ms', 10**9), model)
                desired = [self.state['models'][m]['provider'] + '/' + m for m in sorted(names, key=rank)]
                if len(desired) < 2:
                    continue
                name = 'transcribe-trial' + ('' if role == 'translate' else '-' + role)
                group = next((g for g in self._registry().get('groups', []) if g['id'] == name), {})
                current = [m for m in group.get('members', []) if m in desired and m not in group.get('off', [])]
                if set(current) == set(desired) and current != desired and not group.get('hidden'):
                    self._command('group', 'set', name, 'models-=' + ','.join(current), 'models+=' + ','.join(desired))
                    self._audit('candidate_order', name, current, desired)

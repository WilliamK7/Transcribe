"""Keep the dedicated Bailian trial provider within its confirmed free quota."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .bailian_quota import CLIQuotaSource, VerifiedIdentity, beijing_today, parse_observation
from .common import Cancelled, ToolError, atomic_json
from .mt_backend import key_fingerprint, normalize_mt_url


TRIAL_PROVIDER = 'transcribe-bailian-trial'


class BailianFreeGuard:
    def __init__(self, config, control, *, source=None, clock=time.monotonic):
        self.config, self.control = config, control
        self.source = source or CLIQuotaSource()
        self.clock = clock
        self.lock = threading.RLock()
        self.cached_identity = None
        self.observed_at = None
        self.rows = {}
        self.allowed = set()
        self.revoked_models = set()

    @contextmanager
    def synchronized(self):
        """Wait for the shared quota/configuration state without hiding a stop."""
        self.control.check_start()
        while not self.lock.acquire(timeout=.05):
            self.control.check_start()
        try:
            self.control.check_start()
            yield
        finally:
            self.lock.release()

    def _identity(self, provider):
        record = json.loads((self.config.data / 'bailian-quota-binding.json').read_bytes())
        if (not isinstance(record, dict) or record.get('invalidated') is not False or record.get('source') != 'user_confirmed' or
                type(record.get('version')) is not int or record['version'] not in (1, 2) or
                record.get('region') != 'cn-beijing' or
                record.get('workspace') != self.config.settings.workspace_id or
                provider.get('keys') or provider.get('headers') or provider.get('fallback') or
                not provider.get('countFree') or
                normalize_mt_url(provider.get('chat')) !=
                f"https://{record['workspace']}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" or
                key_fingerprint(provider.get('key', '')) != record.get('key_fingerprint') or
                key_fingerprint(self.config.get_key()) != record.get('key_fingerprint') or
                not isinstance(record.get('cli_fingerprint'), str) or
                re.fullmatch(r'[0-9a-f]{64}', record['cli_fingerprint']) is None):
            raise ValueError()
        models = provider.get('models')
        confirmed = record.get('confirmed_models')
        if (not isinstance(models, list) or not models or not isinstance(confirmed, list) or
                any(not isinstance(m, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', m) is None
                    for m in models)):
            raise ValueError()
        proof = VerifiedIdentity(record['workspace'], record['key_fingerprint'],
                                 record['region'], record['cli_fingerprint'], record['version'])
        if not self.source.bound_to(proof):
            raise ValueError()
        return proof, tuple(m for m in models if m in confirmed and m not in self.revoked_models)

    def _revoke_unprotected(self, models):
        revoked = {m for m in models if m in self.rows and self.rows[m].auto_stop is False}
        if not revoked:
            return
        self.revoked_models.update(revoked)
        path = self.config.data / 'bailian-quota-binding.json'
        record = json.loads(path.read_bytes())
        record['confirmed_models'] = [m for m in record['confirmed_models'] if m not in revoked]
        atomic_json(path, record)

    def _command(self, *args):
        self.control.check_start()
        folder = Path(self.config.settings.magpie_data_dir)
        executable = folder.parent / 'magpie.com'
        if (folder.name != 'data' or not (folder.parent / '.portable').is_file() or not executable.is_file()):
            raise ToolError('无法管理百炼试用入口；请检查 magpie 便携目录。', 'magpie_group')
        process = None
        try:
            self.control.check_start()
            deadline = time.monotonic() + 5
            process = subprocess.Popen([str(executable), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while True:
                # Let an already-started save finish within the same drain
                # window as translation requests; a second Ctrl+C ends it now.
                self.control.check_inflight()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError()
                try:
                    process.wait(timeout=min(.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            # A successful save must not start another mutation after a stop.
            self.control.check_start()
            if process.returncode:
                raise ValueError()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ToolError('无法更新百炼试用保护；未继续发送翻译请求。', 'magpie_group') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                # No pipes or extra drain window: terminate and reap this child.
                process.wait()

    def prepare(self, settings, base_url):
        try:
            self._prepare(settings, base_url)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
            raise ToolError('无法读取或核验百炼试用配置；未继续发送翻译请求。', 'magpie_group') from None

    def _prepare(self, settings, base_url):
        from .magpie import bound_gateway
        with self.synchronized():
            self.allowed = set()
            if not bound_gateway(settings, base_url):
                return
            path = Path(settings.magpie_data_dir) / 'providers.json'
            raw = json.loads(path.read_bytes())
            providers = [p for p in raw['providers'] if p.get('id') == TRIAL_PROVIDER]
            if not providers:
                return
            if len(providers) != 1:
                raise ToolError('百炼试用入口重复。', 'magpie_group')
            provider = providers[0]
            eligible = []
            try:
                proof, models = self._identity(provider)
                identity = (proof, models)
                if identity != self.cached_identity or self.observed_at is None or self.clock() - self.observed_at >= 20:
                    self.rows = {}
                    self.cached_identity, self.observed_at = identity, self.clock()
                    try:
                        observation = self.source.query(control=self.control, timeout=15, proof=proof)
                        self.rows = parse_observation(observation)
                    except Cancelled:
                        raise
                    except Exception:
                        # Failed observations share the same short TTL, so
                        # parallel stages cannot each launch a 15-second query.
                        self.rows = {}
                self.control.check_start()
                self._revoke_unprotected(models)
                proof, models = self._identity(provider)
                today = beijing_today()
                eligible = [m for m in models if m in self.rows and self.rows[m].usable(today)]
            except Cancelled:
                raise
            except Exception:
                self.control.check_start()
                self.rows = {}
                self.cached_identity = None
            group_id = settings.mt_free_model.removeprefix('group/')
            groups = [g for g in raw.get('groups', []) if g.get('id') == group_id]
            if not settings.mt_free_model.startswith('group/') or len(groups) != 1:
                if not provider.get('off'):
                    self._command('provider', 'off', TRIAL_PROVIDER)
                return
            group = groups[0]
            prefix = TRIAL_PROVIDER + '/'
            members = [m for m in group.get('members', []) if not m.startswith(prefix)]
            wanted = members + [prefix + m for m in eligible]
            # Disable first when nothing can be proven. A failed update must
            # never leave an unverified trial available through another group.
            if not eligible and not provider.get('off'):
                self._command('provider', 'off', TRIAL_PROVIDER)
            # magpie resolves group members from enabled providers only.
            # Enable after obtaining proof, then update members; no project
            # request is admitted until the saved configuration is validated.
            if eligible and provider.get('off'):
                self._command('provider', 'on', TRIAL_PROVIDER)
            try:
                if wanted != group.get('members'):
                    if not wanted:
                        raise ToolError('免费组没有可用成员。', 'magpie_group')
                    self._command('group', 'set', group_id, 'models=' + ','.join(wanted))
            except ToolError:
                self.control.check_start()
                if eligible:
                    self._command('provider', 'off', TRIAL_PROVIDER)
                raise
            current = json.loads(path.read_bytes())
            current_provider = next(p for p in current['providers'] if p.get('id') == TRIAL_PROVIDER)
            current_group = next(g for g in current['groups'] if g.get('id') == group_id)
            if eligible and self._identity(current_provider) != (proof, models):
                raise ToolError('百炼试用身份在刷新期间变化。', 'magpie_group')
            if current_group.get('members') != wanted or bool(current_provider.get('off')) == bool(eligible):
                raise ToolError('百炼试用保护更新未生效。', 'magpie_group')
            self.control.check_start()
            self.allowed = {(TRIAL_PROVIDER, m) for m in eligible}

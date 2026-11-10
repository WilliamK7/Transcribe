"""Explicit, resumable migration of project-owned magpie role groups."""
from __future__ import annotations

import itertools
import json
import time
from collections import defaultdict

from .bailian_free import BailianFreeGuard, ROLES, TRIAL_PROVIDER
from .common import ToolError, atomic_json
from .magpie import bound_gateway, validate_role_pools
from .mt_admission import ModelEvaluator, admission_key, canonical_model
from .mt_transport import ProcessTransport


def choose_roles(evaluations, reserved=None):
    """All suppliers of an underlying model belong to exactly one role."""
    families = defaultdict(list)
    for member, result in evaluations.items():
        for model in result.get('actual_models', []):
            families[canonical_model(model)].append((member, result))
    reserved = reserved or {}
    eligible = {role: [name for name, rows in families.items() if reserved.get(name, role) == role and
                       any(role in r.get('roles', []) for _, r in rows)]
                for role in ROLES}
    combinations = [p for p in itertools.product(*(eligible[r] for r in ROLES)) if len(set(p)) == 3]
    if not combinations:
        raise ToolError('尚未验收出三个独立型号；已保存验收结果，未切换角色组。', 'mt_admission')
    def score(names):
        review = families[names[1]]
        quiet = any(r.get('non_thinking', {}).get('review') for _, r in review)
        cost = sum(min(r.get('duration_ms', 10**9) for _, r in families[n] if role in r['roles'])
                   for role, n in zip(ROLES, names))
        return (not quiet, cost, names)
    picked = min(combinations, key=score)
    assigned = dict(zip(picked, ROLES))
    for name in sorted(set(families) - set(assigned)):
        valid = [r for r in ROLES if reserved.get(name, r) == r and any(r in result['roles'] for _, result in families[name])]
        if valid:
            assigned[name] = min(valid, key=lambda role: (sum(v == role for v in assigned.values()), ROLES.index(role)))
    pools = {r: [] for r in ROLES}
    for name, role in assigned.items():
        pools[role].extend(member for member, result in families[name] if role in result['roles'])
    return pools, assigned


class GatewaySetup:
    def __init__(self, config, control, *, manager=None):
        self.config, self.control = config, control
        self.manager = manager or BailianFreeGuard(config, control, job_id='gateway-migration')
        self.path = config.data / 'gateway-admission.json'
        self.state = json.loads(self.path.read_bytes()) if self.path.exists() else {'version': 1, 'evaluations': {}}
        self.managed_path = config.data / 'gateway-managed.json'
        self.managed = (json.loads(self.managed_path.read_bytes()) if self.managed_path.exists()
                        else {'version': 1, 'groups': {}, 'excluded': []})

    def evaluate(self, member, judge):
        raw = self.manager._registry()
        owner, model = member.split('/', 1)
        provider = next(p for p in raw['providers'] if p['id'] == owner)
        if provider.get('off'):
            return None
        key = admission_key(provider, model)
        result = self.state['evaluations'].get(key)
        if result is None or result.get('retry_after') and result['retry_after'] <= time.time():
            result = ModelEvaluator(self.config, self.control, judge=judge).evaluate(member)
            result.update(model=member, evaluated_at=time.time())
            self.state['evaluations'][key] = result
            atomic_json(self.path, self.state)
            self.manager._audit('evaluation_finished', model, None, result['roles'])
        return result

    def openrouter_candidates(self):
        """Official catalog; random routers and non-text models never enter."""
        reply = ProcessTransport().request('GET', 'https://openrouter.ai/api/v1/models', headers={},
                                          control=self.control, timeout=15, drain=False)
        if reply.status != 200:
            return []
        rows = json.loads(reply.body)['data']
        return [row['id'] for row in sorted(rows, key=lambda r: (-r.get('context_length', 0), r['id']))
                if row['id'].endswith(':free') and 'text' in row.get('architecture', {}).get('output_modalities', [])
                and 'text' in row.get('architecture', {}).get('input_modalities', [])
                and row.get('pricing', {}).get('prompt') in ('0', '0.0')
                and row.get('pricing', {}).get('completion') in ('0', '0.0')]

    def _group(self, name, members, routing, *, legacy=()):
        """Incremental CLI transaction; preserve external members and switches."""
        current = next((g for g in self.manager._registry().get('groups', []) if g['id'] == name), None)
        previous = list(dict.fromkeys(self.managed['groups'].get(name, []) + list(legacy)))
        removed = {m for m in previous if current is not None and m not in current.get('members', [])}
        disabled = set(current.get('off', [])) if current else set()
        excluded = set(self.managed['excluded']) | {name + ':' + m for m in removed | disabled}
        self.managed['excluded'] = sorted(excluded)
        members = [m for m in dict.fromkeys(members) if name + ':' + m not in excluded and
                   (current is None or m not in current.get('off', []))]
        if current is None:
            if not members:
                return False
            self.manager._command('group', 'add', name, 'models=' + ','.join(members),
                                  'routing=' + routing, 'stays=off')
        else:
            if current.get('hidden'):
                return False
            additions = [m for m in members if m not in current['members']]
            removals = [m for m in previous if m in current['members'] and m not in members and m not in disabled]
            retained = [m for m in current['members'] if m not in removals]
            if not retained and not additions:
                return False  # magpie disallows empty groups.
            args = (['models+=' + ','.join(additions)] if additions else [])
            args += ['models-=' + ','.join(removals)] if removals else []
            projected = retained + additions
            if routing == 'order' and [m for m in projected if m in members] != members:
                # One atomic CLI delta preserves concurrent unrelated members.
                moved = [m for m in members if m in current['members']]
                args = (['models-=' + ','.join(dict.fromkeys(removals + moved))] if removals or moved else [])
                args += ['models+=' + ','.join(members)] if members else []
            self.manager._command('group', 'set', name, *args, 'routing=' + routing, 'stays=off',
                *(['weight=' + ','.join(m + '=1' for m in retained + additions)] if routing == 'weight' else []))
        self.managed['groups'][name] = members
        atomic_json(self.managed_path, self.managed)
        self.manager._audit('role_group_updated', name, current.get('members', []) if current else [], members)
        return True

    def _retire_shared_trial(self):
        """Keep the binding template while making all old direct references inert."""
        current = next((p for p in self.manager._registry()['providers'] if p['id'] == TRIAL_PROVIDER), None)
        if current is None or current.get('off'):
            return
        self.manager._command('provider', 'off', TRIAL_PROVIDER)
        after = next((p for p in self.manager._registry()['providers'] if p['id'] == TRIAL_PROVIDER), None)
        if after is None or after.get('off') is not True:
            raise ToolError('旧共享试用入口未停用，角色迁移已停止。', 'magpie_group')
        self.manager._audit('legacy_trial_retired', TRIAL_PROVIDER,
                           {'off': bool(current.get('off'))}, {'off': True})

    def apply(self, evaluations, free=()):
        if not bound_gateway(self.config.settings, self.config.settings.mt_base_url):
            raise ToolError('角色迁移只适用于已绑定的本机 magpie。', 'mt_config')
        free = set(free)
        pinned = {canonical_model(model): row['role'] for model, row in self.manager.state['models'].items()
                  if row.get('status') == 'active'}
        pools, assignments = choose_roles({m: e for m, e in evaluations.items() if m not in free}, pinned)
        assignments.update(pinned)
        for member in sorted(free):
            result = evaluations[member]
            actual = result.get('actual_models', [])
            reserved = {assignments[m] for m in actual if m in assignments}
            eligible = [r for r in ROLES if r in result.get('roles', []) and (not reserved or reserved == {r})]
            if eligible:
                role = min(eligible, key=lambda r: (not result.get('non_thinking', {}).get(r),
                    sum(m in free for m in pools[r]), ROLES.index(r)))
                pools[role].append(member)
                assignments.update({m: role for m in actual})
        if any(not set(members) - free for members in pools.values()):
            raise ToolError('每个角色必须有已验收的商业后备。', 'mt_admission')
        backup = self.config.data / 'gateway-backups' / str(time.time_ns())
        backup.mkdir(parents=True)
        atomic_json(backup / 'providers.json', self.manager._registry())
        settings = self.config.data / 'settings.json'
        if settings.exists():
            (backup / 'settings.json').write_bytes(settings.read_bytes())
        self._retire_shared_trial()
        for role, members in pools.items():
            main = 'transcribe' + ('' if role == 'translate' else '-' + role)
            paid_members, free_members = [], []
            for tier, output in ((False, paid_members), (True, free_members)):
                suppliers = defaultdict(list)
                for member in members:
                    if (member in free) == tier:
                        suppliers[member.split('/', 1)[0]].append(member)
                for supplier, models in suppliers.items():
                    name = main + '-' + supplier.removeprefix('transcribe-')
                    if self._group(name, models, 'order'):
                        output.append('group/' + name)
            paid = main + '-paid'
            legacy_paid = ('transcribe-qiniu-paid/z-ai/glm-5.3-flash', 'transcribe-bailian/deepseek-v4.1-flash') if role == 'translate' else ()
            self._group(paid, paid_members, 'weight', legacy=legacy_paid)
            free_name = 'transcribe-free' + ('' if role == 'translate' else '-' + role)
            legacy_free = ('transcribe-qiniu-free/qiniu/conatus-free', 'transcribe-openrouter-free/openrouter/free') if role == 'translate' else ()
            current_free = next((g for g in self.manager._registry().get('groups', []) if g['id'] == free_name), {})
            legacy_free += tuple(m for m in current_free.get('members', []) if m.startswith(TRIAL_PROVIDER + '/'))
            has_free = self._group(free_name, free_members, 'weight', legacy=legacy_free)
            ordered = (['group/' + free_name] if has_free else []) + ['group/' + paid]
            self._group(main, ordered, 'order', legacy=['group/transcribe-free', 'group/transcribe-paid'] if role == 'translate' else ())
        self.manager.state['reserved_models'] = assignments
        self.manager.state['admission_judge'] = pools['judge'][0]
        self.manager._save()
        self.config.settings.mt_routing = {'strategy': 'free_first'}
        self.config.settings.mt_policy_version += 1
        self.config.save()
        validate_role_pools(self.config.settings, {r: 'group/transcribe' + ('' if r == 'translate' else '-' + r) for r in ROLES})
        return {'roles': pools, 'backup': str(backup)}

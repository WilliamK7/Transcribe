"""Explicit, resumable migration of project-owned magpie role groups."""
from __future__ import annotations

import itertools
import copy
import json
import time
from collections import defaultdict
from pathlib import Path

from .bailian_free import BailianFreeGuard, ROLES, TRIAL_PROVIDER
from .common import ToolError, atomic_json
from .magpie import bound_gateway, validate_role_pools
from .mt_admission import ModelEvaluator, admission_key
from .mt_catalog import read_registry, saved_catalog
from .mt_identity import VSR_MEMBER, canonical_model, model_set
from .mt_channel_policy import load_policy, check_provider_policy


def choose_roles(evaluations, reserved=None):
    """All suppliers of an underlying model belong to exactly one role."""
    parents, rows = {}, {}
    def root(name):
        parents.setdefault(name, name)
        if parents[name] != name:
            parents[name] = root(parents[name])
        return parents[name]
    for member, result in evaluations.items():
        names = sorted({canonical_model(m) for m in result.get('actual_models', [])})
        if not names:
            continue
        rows[member] = names, result
        for name in names:
            parents[root(name)] = root(names[0])
    families, components = defaultdict(list), defaultdict(set)
    for member, (names, result) in rows.items():
        key = root(names[0])
        families[key].append((member, result))
        components[key].update(names)
    reserved = reserved or {}
    def permitted(name, role):
        return all(reserved.get(m, role) == role for m in components[name])
    eligible = {role: [name for name, rows in families.items() if permitted(name, role) and
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
        valid = [r for r in ROLES if permitted(name, r) and any(r in result['roles'] for _, result in families[name])]
        if valid:
            assigned[name] = min(valid, key=lambda role: (sum(v == role for v in assigned.values()), ROLES.index(role)))
    pools = {r: [] for r in ROLES}
    for name, role in assigned.items():
        pools[role].extend(member for member, result in families[name] if role in result['roles'])
    return pools, {model: role for name, role in assigned.items() for model in components[name]}


class GatewaySetup:
    def __init__(self, config, control, *, manager=None):
        self.config, self.control = config, control
        self.policy = load_policy(config)
        self.manager = manager or BailianFreeGuard(config, control, job_id='gateway-migration')
        self.path = config.data / 'gateway-admission.json'
        self.state = json.loads(self.path.read_bytes()) if self.path.exists() else {'version': 1, 'evaluations': {}}
        self.managed_path = config.data / 'gateway-managed.json'
        self.plan_path = config.data / 'gateway-apply-pending.json'
        self.managed = (json.loads(self.managed_path.read_bytes()) if self.managed_path.exists()
                        else {'version': 1, 'groups': {}, 'excluded': []})
        self.catalog = saved_catalog(config) or {'version': 1, 'models': {}, 'identities': {}}
        self._staged = None
        self._actions = []

    def _registry(self):
        return self._staged if self._staged is not None else self.manager._registry()

    def _command(self, *args):
        if self._staged is None:
            return self.manager._command(*args)
        self._actions.append(args)
        group = next((g for g in self._staged['groups'] if g['id'] == args[2]), None)
        if group is None:
            group = {'id': args[2], 'members': []}
            self._staged['groups'].append(group)
        for arg in args[3:]:
            key, value = arg.split('=', 1)
            if key == 'models':
                group['members'] = value.split(',')
            elif key == 'models+':
                group['members'] += [m for m in value.split(',') if m not in group['members']]
            elif key == 'models-':
                group['members'] = [m for m in group['members'] if m not in value.split(',')]
            elif key == 'routing':
                group['routing'] = value
            elif key == 'stays':
                group['affinity'] = value
            elif key == 'weight':
                group['weights'] = {m: float(w) for m, w in (item.rsplit('=', 1) for item in value.split(','))}

    def evaluate(self, member, judge):
        key, result = self.cached_evaluation(member, judge)
        if key is None:
            return None
        if result is None or result.get('retry_after') and result['retry_after'] <= time.time():
            result = ModelEvaluator(self.config, self.control, judge=judge,
                                    identities=self.catalog.get('identities', {})).evaluate(member)
            result.update(model=member, evaluated_at=time.time())
            self.state['evaluations'][key] = result
            atomic_json(self.path, self.state)
            self.manager._audit('evaluation_finished', member, None, result['roles'])
        return result

    def cached_evaluation(self, member, judge):
        check_provider_policy(self.config, [member, judge], policy=self.policy)
        raw = read_registry(self.config.settings, self.catalog, policy=self.policy)
        owner, model = member.split('/', 1)
        provider = next(p for p in raw['providers'] if p['id'] == owner)
        if provider.get('off'):
            return None, None
        provider = {**provider, '_identities': self.catalog.get('identities', {}), '_admission_judge': judge}
        judge_owner, judge_name = judge.split('/', 1)
        judge_provider = next(p for p in raw['providers'] if p['id'] == judge_owner)
        provider['_judge_fingerprint'] = admission_key(judge_provider, judge_name)
        key = admission_key(provider, model)
        return key, self.state['evaluations'].get(key)

    def _validated_groups(self, registry, entries):
        """Validate both role identity and every commercial fallback before writes."""
        from .magpie import group_models
        from .mt_catalog import offer_billing
        check_provider_policy(self.config, list(entries.values()) + [m + '-paid' for m in entries.values()],
                              policy=self.policy, registry=registry)
        validate_role_pools(self.config.settings, entries, identities=self.catalog.get('identities', {}), registry=registry)
        for entry in entries.values():
            leaves = group_models(registry, entry + '-paid')
            if not leaves or any(self.catalog.get('identities', {}).get(owner + '/' + name, {}).get('billing',
                offer_billing(provider, name)) != 'commercial' for owner, name, provider in leaves):
                raise ToolError('商业后备为空或包含免费／试用型号，未完成迁移。', 'mt_admission')

    def independent_judge(self, member):
        identities = self.catalog.get('identities', {})
        candidates = list(self.policy['judges'])
        candidates.extend(m for m, (role, _) in self.current_members().items() if role in {'review', 'judge'})
        raw = read_registry(self.config.settings, self.catalog, policy=self.policy)
        enabled = {p['id']: p for p in raw['providers'] if not p.get('off') and p['id'] not in self.policy['excluded_providers']}
        for candidate in candidates:
            owner, name = candidate.split('/', 1)
            if (owner in enabled and name not in enabled[owner].get('modelsOff', []) and
                    not model_set(member, identities) & model_set(candidate, identities)):
                return candidate
        raise ToolError('找不到与候选底层型号独立的验收评判者。', 'mt_admission')

    def current_members(self):
        from .magpie import group_models
        raw = read_registry(self.config.settings, self.catalog, policy=self.policy)
        groups = {g['id'] for g in raw.get('groups', [])}
        members = {}
        for role in ROLES:
            main = 'transcribe' + ('' if role == 'translate' else '-' + role)
            for group in (main, main + '-paid'):
                if group not in groups:
                    continue
                for owner, name, provider in group_models(raw, 'group/' + group):
                    if owner + '/' + name in members and members[owner + '/' + name][0] != role:
                        raise ToolError('同一网关成员已跨角色使用，未修改分组。', 'mt_admission')
                    members[owner + '/' + name] = (role, provider)
        return members

    def _group(self, name, members, routing, *, legacy=()):
        """Incremental CLI transaction; preserve external members and switches."""
        current = next((g for g in self._registry().get('groups', []) if g['id'] == name), None)
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
            self._command('group', 'add', name, 'models=' + ','.join(members),
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
            if current.get('routing') != routing:
                args.append('routing=' + routing)
            if current.get('affinity') != 'off':
                args.append('stays=off')
            if routing == 'weight' and any(current.get('weights', {}).get(m, 1) != 1 for m in projected):
                args.append('weight=' + ','.join(m + '=1' for m in projected))
            if args:
                self._command('group', 'set', name, *args)
        self.managed['groups'][name] = members
        if self._staged is None:
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

    @staticmethod
    def _group_state(group):
        # Ignore Magpie's extra display fields, as in post-command validation.
        return None if group is None else {k: group.get(k) for k in ('members', 'routing', 'affinity', 'off', 'hidden')}

    def _recover_pending(self):
        """Reconcile ownership only; never replay an obsolete routing decision."""
        if not self.plan_path.exists():
            return False
        try:
            plan = json.loads(self.plan_path.read_bytes())
            if plan.get('status') == 'complete':
                return False
            if plan.get('status') == 'recovered':
                return True  # Ownership was committed; publication still needs finishing.
            groups, completed = plan['groups'], plan['completed']
            if (not isinstance(groups, dict) or not isinstance(completed, list) or
                    any(not isinstance(name, str) or not isinstance(members, list) or
                        any(not isinstance(m, str) for m in members) for name, members in groups.items()) or
                    any(not isinstance(name, str) or name not in groups for name in completed)):
                raise ValueError()
            applied = set(completed)
            live = {g['id']: g for g in self.manager._registry().get('groups', [])}
            if plan.get('version', 1) == 1:
                # Older journals only confirm completed steps. An unrecorded
                # change cannot safely be attributed to either us or the user.
                backup = Path(plan['backup']).resolve()
                if not backup.is_relative_to((self.config.data / 'gateway-backups').resolve()):
                    raise ValueError()
                before = {g['id']: g for g in json.loads((backup / 'providers.json').read_bytes()).get('groups', [])}
                if any(self._group_state(live.get(name)) != self._group_state(before.get(name))
                       for name in groups.keys() - applied):
                    raise ValueError()
            elif plan['version'] == 2:
                active = plan.get('active')
                if active is not None:
                    name = active['group']
                    if name not in groups or not isinstance(active['after'], dict):
                        raise ValueError()
                    actual = self._group_state(live.get(name))
                    if actual == active['after']:
                        applied.add(name)  # The CLI committed before its reply/checkpoint was lost.
                    elif actual != active['before']:
                        raise ValueError()  # Preserve ambiguous concurrent edits for inspection.
            else:
                raise ValueError()
            recovered = copy.deepcopy(self.managed)
            for name in applied:
                recovered['groups'][name] = groups[name]
            exclusions = plan.get('excluded', [])
            if not isinstance(exclusions, list) or any(not isinstance(e, str) for e in exclusions):
                raise ValueError()
            recovered['excluded'] = sorted(set(recovered['excluded']) | {
                e for e in exclusions if e.split(':', 1)[0] in applied})
            # Let _group see any later manual deletion/disable against the
            # ownership actually committed, not the unexecuted target plan.
            atomic_json(self.managed_path, recovered)
            self.managed = recovered
            atomic_json(self.plan_path, {**plan, 'status': 'recovered'})
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ToolError('未完成的分组更新无法确认归属；已保留迁移记录和备份，未继续修改分组。',
                            'magpie_group') from None

    def apply(self, evaluations, free=()):
        if not bound_gateway(self.config.settings, self.config.settings.mt_base_url):
            raise ToolError('角色迁移只适用于已绑定的本机 magpie。', 'mt_config')
        recovering = self._recover_pending()
        free = set(free)
        pinned = dict(self.manager.state.get('reserved_models', {}))
        for model, row in self.manager.state['models'].items():
            if row.get('status') == 'active':
                identity = canonical_model(model)
                if identity in pinned and pinned[identity] != row['role']:
                    raise ToolError('试用型号与已保留角色冲突，未修改分组。', 'mt_admission')
                pinned[identity] = row['role']
        for member, (role, _) in self.current_members().items():
            try:
                for model in model_set(member, self.catalog.get('identities', {})):
                    if model in pinned and pinned[model] != role:
                        raise ToolError('现有角色型号冲突，未修改分组。', 'mt_admission')
                    pinned[model] = role
            except ValueError:
                continue  # Retire old unverified random-router members below.
        if VSR_MEMBER in evaluations and 'translate' in evaluations[VSR_MEMBER].get('roles', []):
            observed = evaluations[VSR_MEMBER].get('observed_models', [])
            if not observed:
                raise ToolError('动态翻译没有已通过验收的实际后端。', 'mt_admission')
            self.catalog['identities'][VSR_MEMBER]['admitted'] = sorted(set(observed))
            for model in evaluations[VSR_MEMBER]['actual_models']:
                if model in pinned and pinned[model] != 'translate':
                    raise ToolError('动态翻译后端与现有试用角色冲突，未修改分组。', 'mt_admission')
                pinned[model] = 'translate'
        pools, assignments = choose_roles({m: e for m, e in evaluations.items() if m not in free}, pinned)
        assignments.update(pinned)
        for member in sorted(free):
            result = evaluations[member]
            actual = [canonical_model(m) for m in result.get('actual_models', [])]
            reserved = {assignments[m] for m in actual if m in assignments}
            eligible = [r for r in ROLES if r in result.get('roles', []) and (not reserved or reserved == {r})]
            if eligible:
                role = min(eligible, key=lambda r: (not result.get('non_thinking', {}).get(r),
                    sum(m in free for m in pools[r]), ROLES.index(r)))
                pools[role].append(member)
                assignments.update({m: role for m in actual})
        if any(not set(members) - free for members in pools.values()):
            raise ToolError('每个角色必须有已验收的商业后备。', 'mt_admission')
        previous_managed = copy.deepcopy(self.managed)
        self._staged = copy.deepcopy(self.manager._registry())
        self._staged.setdefault('groups', [])
        self._actions = []
        try:
            for provider in self._staged['providers']:
                if provider['id'] == TRIAL_PROVIDER:
                    provider['off'] = True
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
                        legacy_tiers = {'transcribe-bailian': False, 'transcribe-qiniu-paid': False,
                                        'transcribe-qiniu-free': True, 'transcribe-openrouter-free': True}
                        if supplier not in legacy_tiers or tier != legacy_tiers[supplier]:
                            name += '-free' if tier else '-paid'
                        if self._group(name, models, 'order'):
                            output.append('group/' + name)
                paid = main + '-paid'
                legacy_paid = ('transcribe-qiniu-paid/z-ai/glm-5.3-flash', 'transcribe-bailian/deepseek-v4.1-flash') if role == 'translate' else ()
                self._group(paid, paid_members, 'weight', legacy=legacy_paid)
                free_name = 'transcribe-free' + ('' if role == 'translate' else '-' + role)
                legacy_free = ('transcribe-qiniu-free/qiniu/conatus-free', 'transcribe-openrouter-free/openrouter/free') if role == 'translate' else ()
                current_free = next((g for g in self._registry().get('groups', []) if g['id'] == free_name), {})
                legacy_free += tuple(m for m in current_free.get('members', []) if m.startswith(TRIAL_PROVIDER + '/'))
                has_free = self._group(free_name, free_members, 'weight', legacy=legacy_free)
                ordered = (['group/' + free_name] if has_free else []) + ['group/' + paid]
                self._group(main, ordered, 'order', legacy=['group/transcribe-free', 'group/transcribe-paid'] if role == 'translate' else ())
            entries = {r: 'group/transcribe' + ('' if r == 'translate' else '-' + r) for r in ROLES}
            projected = read_registry(self.config.settings, self.catalog)
            projected['groups'] = self._staged['groups']
            for provider in projected['providers']:
                if provider['id'] == TRIAL_PROVIDER:
                    provider['off'] = True
            self._validated_groups(projected, entries)
            from .magpie import group_models
            for role, entry in entries.items():
                for owner, name, _ in group_models(projected, entry):
                    assignments.update({model: role for model in model_set(owner + '/' + name,
                                        self.catalog.get('identities', {}))})
        except Exception:
            self.managed = previous_managed
            raise
        finally:
            self._staged = None
        legacy_active = any(p['id'] == TRIAL_PROVIDER and not p.get('off')
                            for p in self.manager._registry()['providers'])
        if (not recovering and not self._actions and not legacy_active and self.catalog == saved_catalog(self.config) and
                self.config.settings.mt_routing == {'strategy': 'free_first'} and
                self.manager.state.get('reserved_models') == assignments):
            if self.managed != previous_managed:
                atomic_json(self.managed_path, self.managed)
            return {'roles': pools, 'backup': None, 'changed': False}
        target_managed = self.managed
        self.managed = previous_managed  # A failed attempt must not expose uncommitted ownership in memory.
        backup = self.config.data / 'gateway-backups' / str(time.time_ns())
        backup.mkdir(parents=True)
        atomic_json(backup / 'providers.json', self.manager._registry())
        for filename in ('settings.json', 'gateway-managed.json', 'gateway-catalog.json', 'bailian-trial-state.json',
                         'gateway-apply-pending.json'):
            path = self.config.data / filename
            if path.exists():
                (backup / filename).write_bytes(path.read_bytes())
        self._retire_shared_trial()
        # Parent entrypoints switch only after every child has been built.
        roots = {m.removeprefix('group/') for m in entries.values()}
        plan = {'version': 2, 'status': 'pending', 'backup': str(backup),
                'groups': target_managed['groups'], 'excluded': target_managed['excluded'],
                'completed': [], 'active': None}
        atomic_json(self.plan_path, plan)
        children_checked = False
        for command in sorted(self._actions, key=lambda cmd: cmd[2] in roots):
            if command[2] in roots and not children_checked:
                live = read_registry(self.config.settings, self.catalog)
                # Inspect the children actually written, with only the future
                # parent links substituted. Do not publish unverified children.
                live['groups'] = [g for g in live['groups'] if g['id'] not in roots] + [
                    g for g in projected['groups'] if g['id'] in roots]
                self._validated_groups(live, entries)
                children_checked = True
            before = next((g for g in self.manager._registry()['groups'] if g['id'] == command[2]), None)
            expected = next(g for g in projected['groups'] if g['id'] == command[2])
            plan['active'] = {'group': command[2], 'before': self._group_state(before), 'after': self._group_state(expected)}
            atomic_json(self.plan_path, plan)  # Intent must survive a committed command with no reply.
            self.manager._command(*command)
            actual = next((g for g in self.manager._registry()['groups'] if g['id'] == command[2]), None)
            if self._group_state(actual) != self._group_state(expected):
                raise ToolError('子组写入后与预演不一致；已保留备份和迁移记录。', 'magpie_group')
            plan['completed'].append(command[2])
            plan['active'] = None
            atomic_json(self.plan_path, plan)
            self.manager._audit('role_group_updated', command[2], None, target_managed['groups'].get(command[2], []))
        self._validated_groups(read_registry(self.config.settings, self.catalog), entries)
        atomic_json(self.managed_path, target_managed)
        self.managed = target_managed
        self.manager.state['reserved_models'] = assignments
        self.manager.state['admission_judge'] = pools['judge'][0]
        self.manager._save()
        self.config.settings.mt_routing = {'strategy': 'free_first'}
        self.config.settings.mt_policy_version += 1
        self.config.save()
        atomic_json(self.config.data / 'gateway-catalog.json', self.catalog)
        atomic_json(self.plan_path, {**plan, 'status': 'complete'})
        return {'roles': pools, 'backup': str(backup), 'changed': True}

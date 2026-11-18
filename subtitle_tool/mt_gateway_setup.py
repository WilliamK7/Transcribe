"""Resumable updates of project-owned Magpie role groups."""
from __future__ import annotations

import itertools
import copy
import json
import time
from collections import defaultdict

from .bailian_state import TrialState, TRIAL_PROVIDER
from .magpie_admin import MagpieAdmin
from .common import ToolError, atomic_json
from .magpie import bound_gateway, supplies_model, validate_role_pools
from .mt_admission import ModelEvaluator, admission_key, current_role_members, select_admission_judge
from .mt_catalog import read_registry, saved_catalog
from .mt_identity import VSR_MEMBER, canonical_model, model_set
from .mt_channel_policy import ROLES, load_policy, check_provider_policy


def _supplier_group(main, supplier, free):
    name = main + '-' + supplier.removeprefix('transcribe-')
    default_tiers = {'transcribe-bailian': False, 'transcribe-qiniu-paid': False,
                     'transcribe-qiniu-free': True, 'transcribe-openrouter-free': True}
    return name if supplier in default_tiers and free == default_tiers[supplier] else name + ('-free' if free else '-paid')


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


class GroupPlan:
    """Project owned membership and CLI deltas on a private copy of the registry."""
    def __init__(self, groups, ownership):
        self.groups = {g['id']: copy.deepcopy(g) for g in groups}
        self.ownership = copy.deepcopy(ownership)
        self.changes = []

    def set_group(self, name, members, routing):
        current = self.groups.get(name)
        owned = self.ownership['groups'].get(name, [])
        present = current['members'] if current else []
        disabled = set(current.get('off', [])) if current else set()
        removed = set(owned) - set(present) if current is not None else set()
        excluded = set(self.ownership['excluded']) | {name + ':' + m for m in removed | disabled}
        self.ownership['excluded'] = sorted(excluded)
        desired = [m for m in dict.fromkeys(members) if name + ':' + m not in excluded]
        if current is None:
            if not desired:
                return False
            projected = {'id': name, 'members': desired, 'routing': routing, 'affinity': 'off'}
            command = ('group', 'add', name, 'models=' + ','.join(desired), 'routing=' + routing, 'stays=off')
        else:
            if current.get('hidden'):
                return False
            additions = [m for m in desired if m not in present]
            removals = [m for m in owned if m in present and m not in desired and m not in disabled]
            target = [m for m in present if m not in removals] + additions
            if not target:
                return False  # Magpie requires at least one member.
            if routing == 'order' and [m for m in target if m in desired] != desired:
                removals.extend(m for m in desired if m in present)
                additions = desired
                target = [m for m in present if m not in removals] + additions
            args = []
            if removals:
                args.append('models-=' + ','.join(removals))
            if additions:
                args.append('models+=' + ','.join(additions))
            if current.get('routing') != routing:
                args.append('routing=' + routing)
            if current.get('affinity') != 'off':
                args.append('stays=off')
            projected = {**current, 'members': target, 'routing': routing, 'affinity': 'off'}
            if routing == 'weight' and any(current.get('weights', {}).get(m, 1) != 1 for m in target):
                projected['weights'] = dict.fromkeys(target, 1)
                args.append('weight=' + ','.join(m + '=1' for m in target))
            command = ('group', 'set', name, *args) if args else None
        self.groups[name] = projected
        self.ownership['groups'][name] = desired
        if command:
            self.changes.append((command, projected))
        return True


class GatewaySetup:
    def __init__(self, config, control, *, admin=None, trial_state=None):
        self.config, self.control = config, control
        self.policy = load_policy(config)
        self.trial_state = trial_state if trial_state is not None else TrialState(config, lock=admin.lock if admin else None)
        self.admin = admin or MagpieAdmin(config, control, job_id='gateway-setup', lock=self.trial_state.lock)
        if self.admin.lock is not self.trial_state.lock:
            raise ValueError('Gateway administration and role reservations must share a lock')
        self.path = config.data / 'gateway-admission.json'
        self.state = json.loads(self.path.read_bytes()) if self.path.exists() else {'version': 1, 'evaluations': {}}
        self.managed_path = config.data / 'gateway-managed.json'
        self.plan_path = config.data / 'gateway-apply-pending.json'
        self.managed = (json.loads(self.managed_path.read_bytes()) if self.managed_path.exists()
                        else {'version': 1, 'groups': {}, 'excluded': []})
        self.catalog = saved_catalog(config) or {'version': 1, 'models': {}, 'identities': {}}

    def _audit(self, reason, model='', before=None, after=None):
        if not self.admin.audit(reason, model, before, after):
            self.state['audit_status'] = 'write_failed'

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
            self._audit('evaluation_finished', member, None, result['roles'])
        return result

    def cached_evaluation(self, member, judge):
        check_provider_policy(self.config, [member, judge], policy=self.policy)
        raw = read_registry(self.config.settings, self.catalog, policy=self.policy)
        owner, model = member.split('/', 1)
        provider = next(p for p in raw['providers'] if p['id'] == owner)
        if not supplies_model(provider, model):
            return None, None
        provider = {**provider, '_identities': self.catalog.get('identities', {}), '_admission_judge': judge}
        judge_owner, judge_name = judge.split('/', 1)
        judge_provider = next(p for p in raw['providers'] if p['id'] == judge_owner)
        if not supplies_model(judge_provider, judge_name):
            return None, None
        provider['_judge_fingerprint'] = admission_key(judge_provider, judge_name)
        key = admission_key(provider, model)
        return key, self.state['evaluations'].get(key)

    def cached_candidate(self, member):
        # Search only independently verifiable keys, never accept a model name
        # match from a different account, interface or dynamic backend set.
        judges = list(self.policy['judges'])
        judges.extend(m for m, (role, _) in self.current_members().items() if role in {'review', 'judge'})
        for judge in dict.fromkeys(judges):
            if judge.split('/', 1)[0] in self.policy.get('excluded_providers', []):
                continue
            try:
                if model_set(member, self.catalog['identities']) & model_set(judge, self.catalog['identities']):
                    continue
                key, result = self.cached_evaluation(member, judge)
                if result is not None:
                    return key, result, judge
            except (KeyError, ValueError, StopIteration):
                continue
        return None, None, None

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
                raise ToolError('商业后备为空或包含免费／试用型号，未更新分组。', 'mt_admission')

    def independent_judge(self, member):
        return select_admission_judge(self.config, member, self.catalog, policy=self.policy)

    def current_members(self):
        return current_role_members(read_registry(self.config.settings, self.catalog, policy=self.policy))

    def _disable_trial_template(self):
        """Keep the account template disabled; trials use model-specific providers."""
        current = next((p for p in self.admin.registry()['providers'] if p['id'] == TRIAL_PROVIDER), None)
        if current is None or current.get('off'):
            return
        self.admin.command('provider', 'off', TRIAL_PROVIDER, control=self.control)
        after = next((p for p in self.admin.registry()['providers'] if p['id'] == TRIAL_PROVIDER), None)
        if after is None or after.get('off') is not True:
            raise ToolError('试用账户模板未停用，分组更新已停止。', 'magpie_group')
        self._audit('trial_template_disabled', TRIAL_PROVIDER,
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
            live = {g['id']: g for g in self.admin.registry().get('groups', [])}
            if plan.get('version') != 2:
                raise ValueError()
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
            recovered = copy.deepcopy(self.managed)
            for name in applied:
                recovered['groups'][name] = groups[name]
            exclusions = plan.get('excluded', [])
            if not isinstance(exclusions, list) or any(not isinstance(e, str) for e in exclusions):
                raise ValueError()
            recovered['excluded'] = sorted(set(recovered['excluded']) | {
                e for e in exclusions if e.split(':', 1)[0] in applied})
            # Let planning see any later manual deletion/disable against the
            # ownership actually committed, not the unexecuted target plan.
            atomic_json(self.managed_path, recovered)
            self.managed = recovered
            atomic_json(self.plan_path, {**plan, 'status': 'recovered'})
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ToolError('未完成的分组更新无法确认归属；已保留更新记录和备份。',
                            'magpie_group') from None

    def _role_constraints(self, evaluations):
        """Inspect current reservations without moving roles or granting admission."""
        pinned = dict(self.trial_state.data.get('reserved_models', {}))
        for model, row in self.trial_state.data['models'].items():
            if row.get('status') == 'active':
                identity = canonical_model(model)
                if identity in pinned and pinned[identity] != row['role']:
                    raise ToolError('试用型号与已保留角色冲突，未修改分组。', 'mt_admission')
                pinned[identity] = row['role']
        current = self.current_members()
        for member, (role, _) in current.items():
            if member == VSR_MEMBER:
                continue  # Pin established roles before checking the dynamic backend set.
            try:
                for model in model_set(member, self.catalog.get('identities', {})):
                    if model in pinned and pinned[model] != role:
                        raise ToolError('现有角色型号冲突，未修改分组。', 'mt_admission')
                    pinned[model] = role
            except ValueError:
                continue  # Retire old unverified random-router members below.
        dynamic = evaluations.get(VSR_MEMBER, {})
        qualified = 'translate' in dynamic.get('roles', [])
        retired = []
        if VSR_MEMBER in current or qualified:
            role = current.get(VSR_MEMBER, ('translate', None))[0]
            models = set(self.catalog['identities'].get(VSR_MEMBER, {}).get('models', []))
            if qualified:
                models.update(canonical_model(m) for m in dynamic['actual_models'])
            if any(pinned.get(model, role) != role for model in models) or qualified and role != 'translate':
                retired.append(VSR_MEMBER)
            else:
                pinned.update({model: role for model in models})
        return pinned, retired

    def _plan_groups(self, pools, free, retired, assignments):
        projected = read_registry(self.config.settings, self.catalog)
        plan = GroupPlan(projected.get('groups', []), self.managed)
        for role, members in pools.items():
            main = 'transcribe' + ('' if role == 'translate' else '-' + role)
            paid_members, free_members = [], []
            for tier, output in ((False, paid_members), (True, free_members)):
                suppliers = defaultdict(list)
                for member in members:
                    if (member in free) == tier:
                        suppliers[member.split('/', 1)[0]].append(member)
                # Revisit owned supplier groups even if their last qualified
                # model was retired. Preserve reachable manual siblings.
                for member in retired:
                    supplier = member.split('/', 1)[0]
                    if _supplier_group(main, supplier, tier) in self.managed['groups']:
                        suppliers.setdefault(supplier, [])
                for supplier, models in suppliers.items():
                    name = _supplier_group(main, supplier, tier)
                    if plan.set_group(name, models, 'order'):
                        output.append('group/' + name)
            paid = main + '-paid'
            plan.set_group(paid, paid_members, 'weight')
            free_name = 'transcribe-free' + ('' if role == 'translate' else '-' + role)
            has_free = plan.set_group(free_name, free_members, 'weight')
            ordered = (['group/' + free_name] if has_free else []) + ['group/' + paid]
            plan.set_group(main, ordered, 'order')
        entries = {r: 'group/transcribe' + ('' if r == 'translate' else '-' + r) for r in ROLES}
        projected['groups'] = list(plan.groups.values())
        for provider in projected['providers']:
            if provider['id'] == TRIAL_PROVIDER:
                provider['off'] = True
        from .magpie import group_models
        if retired and any(owner + '/' + name in retired for entry in entries.values()
                           for endpoint in (entry, entry + '-paid')
                           for owner, name, _ in group_models(projected, endpoint)):
            raise ToolError('动态翻译后端与其他角色冲突，无法从托管分组安全移出入口。', 'mt_dynamic_role_conflict')
        self._validated_groups(projected, entries)
        for role, entry in entries.items():
            for owner, name, _ in group_models(projected, entry):
                assignments.update({model: role for model in model_set(owner + '/' + name,
                                    self.catalog.get('identities', {}))})
        return plan, projected, entries

    def _execute_groups(self, group_plan, projected, entries, plan):
        roots = {m.removeprefix('group/') for m in entries.values()}
        children_checked = False
        for command, expected in sorted(group_plan.changes, key=lambda change: change[0][2] in roots):
            if command[2] in roots and not children_checked:
                live = read_registry(self.config.settings, self.catalog)
                # Inspect the children actually written, with only the future
                # parent links substituted. Do not publish unverified children.
                live['groups'] = [g for g in live['groups'] if g['id'] not in roots] + [
                    g for g in projected['groups'] if g['id'] in roots]
                self._validated_groups(live, entries)
                children_checked = True
            before = next((g for g in self.admin.registry()['groups'] if g['id'] == command[2]), None)
            plan['active'] = {'group': command[2], 'before': self._group_state(before), 'after': self._group_state(expected)}
            atomic_json(self.plan_path, plan)  # Intent must survive a committed command with no reply.
            self.admin.command(*command, control=self.control)
            actual = next((g for g in self.admin.registry()['groups'] if g['id'] == command[2]), None)
            if self._group_state(actual) != self._group_state(expected):
                raise ToolError('子组写入后与预演不一致；已保留备份和更新记录。', 'magpie_group')
            plan['completed'].append(command[2])
            plan['active'] = None
            atomic_json(self.plan_path, plan)
            self._audit('role_group_updated', command[2], None, group_plan.ownership['groups'][command[2]])
        self._validated_groups(read_registry(self.config.settings, self.catalog), entries)

    def apply(self, evaluations, free=()):
        with self.admin.lock:
            return self._apply(evaluations, free)

    def _apply(self, evaluations, free):
        if not bound_gateway(self.config.settings, self.config.settings.mt_base_url):
            raise ToolError('角色分组更新需要已绑定的本机 magpie。', 'mt_config')
        self.trial_state.read()  # A cached snapshot cannot excuse corrupt on-disk reservations.
        recovering = self._recover_pending()
        pinned, retired = self._role_constraints(evaluations)
        free = set(free) - set(retired)
        evaluations = {m: e for m, e in evaluations.items() if m not in retired}
        dynamic = evaluations.get(VSR_MEMBER, {})
        qualified = 'translate' in dynamic.get('roles', [])
        if qualified and not retired:
            observed = dynamic.get('observed_models', [])
            if not observed:
                raise ToolError('动态翻译没有已通过验收的实际后端。', 'mt_admission')
            self.catalog['identities'][VSR_MEMBER]['admitted'] = sorted(set(observed))
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
        group_plan, projected, entries = self._plan_groups(pools, free, retired, assignments)
        template_active = any(p['id'] == TRIAL_PROVIDER and not p.get('off')
                              for p in self.admin.registry()['providers'])
        if (not recovering and not group_plan.changes and not template_active and self.catalog == saved_catalog(self.config) and
                self.trial_state.data.get('reserved_models') == assignments):
            if group_plan.ownership != self.managed:
                atomic_json(self.managed_path, group_plan.ownership)
                self.managed = group_plan.ownership
            return {'roles': pools, 'backup': None, 'changed': False, 'role_conflicts': retired}
        target_managed = group_plan.ownership
        backup = self.config.data / 'gateway-backups' / str(time.time_ns())
        backup.mkdir(parents=True)
        atomic_json(backup / 'providers.json', self.admin.registry())
        for filename in ('settings.json', 'gateway-managed.json', 'gateway-catalog.json', 'bailian-trial-state.json',
                         'gateway-apply-pending.json'):
            path = self.config.data / filename
            if path.exists():
                (backup / filename).write_bytes(path.read_bytes())
        self._disable_trial_template()
        # Parent entrypoints switch only after every child has been built.
        plan = {'version': 2, 'status': 'pending', 'backup': str(backup),
                'groups': target_managed['groups'], 'excluded': target_managed['excluded'],
                'completed': [], 'active': None}
        atomic_json(self.plan_path, plan)
        self._execute_groups(group_plan, projected, entries, plan)
        atomic_json(self.managed_path, target_managed)
        self.managed = target_managed
        self.trial_state.data['reserved_models'] = assignments
        self.trial_state.data['admission_judge'] = pools['judge'][0]
        self.trial_state.save(self.control)
        self.config.settings.mt_policy_version += 1
        self.config.save()
        atomic_json(self.config.data / 'gateway-catalog.json', self.catalog)
        atomic_json(self.plan_path, {**plan, 'status': 'complete'})
        return {'roles': pools, 'backup': str(backup), 'changed': True, 'role_conflicts': retired}

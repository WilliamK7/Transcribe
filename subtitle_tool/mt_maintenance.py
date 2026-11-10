"""Bounded, incremental maintenance of explicitly enrolled gateway providers."""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone

from .bailian_free import BailianFreeGuard, _BackgroundCancel
from .common import Cancelled, ToolError, atomic_json
from .mt_admission import admission_key
from .mt_catalog import Catalog, offer_billing, read_registry
from .mt_channel_policy import candidate_allowed, load_policy, check_provider_policy
from .mt_control import MTControl
from .mt_gateway_setup import GatewaySetup
from .mt_identity import canonical_model, model_set


def retained_result(previous, result, *, same_identity):
    if same_identity and previous and result.get('retry_after'):
        return previous
    return result


def group_role(name):
    for role in ('review', 'judge'):
        if name == 'transcribe-' + role or name.startswith('transcribe-' + role + '-') or name == 'transcribe-free-' + role:
            return role
    return 'translate'


def manually_held(member, current, setup, registry):
    """A deletion in a former role does not ban the same model's assigned role."""
    roles = {current[member][0]} if member in current else {
        setup.manager.state.get('reserved_models', {}).get(m) for m in model_set(member, setup.catalog['identities'])}
    roles.discard(None)
    if not roles:
        roles = {'translate', 'review', 'judge'}
    for group in registry.get('groups', []):
        if group_role(group['id']) in roles and (group.get('hidden') and member in group.get('members', []) or
                                                member in group.get('off', [])):
            return True
    return any(e.endswith(':' + member) and group_role(e.split(':', 1)[0]) in roles for e in setup.managed['excluded'])


def identity_key(member, registry, catalog):
    owner, name = member.split('/', 1)
    provider = next((p for p in registry['providers'] if p['id'] == owner), None)
    if provider is None:
        return None
    identity = catalog.get('identities', {}).get(member, {
        'models': [canonical_model(member)], 'dynamic': False, 'billing': offer_billing(provider, name)})
    row = provider.get('_models', {}).get(name, {})
    descriptive = {'id', 'name', 'free', 'pricing', 'cost', 'released', 'description', 'rate', 'rateWas',
                   'modalities', 'inputs', 'outputs'}
    capabilities = {k: v for k, v in row.items() if k not in descriptive}
    capabilities['input_modalities'] = row.get('inputs', row.get('modalities', {}).get('input', ['text']))
    capabilities['output_modalities'] = row.get('outputs', row.get('modalities', {}).get('output', ['text']))
    provider = {**provider, '_models': {name: capabilities}}
    return admission_key({**provider, '_identities': {member: identity}}, name)


class ChannelMaintenance:
    """One worker inside the process lock; share trial's lock only during writes."""
    def __init__(self, config, control, *, trial=None):
        self.config = config
        self.stop = threading.Event()
        self.control = MTControl(_BackgroundCancel(control, self.stop), drain_seconds=0)
        self.trial = trial or BailianFreeGuard(config, self.control, job_id='channel-maintenance')
        self.owns_trial = trial is None
        self.lock = threading.Lock()
        self.thread = None
        self.path = config.data / 'gateway-maintenance.json'
        try:
            self.state = json.loads(self.path.read_bytes()) if self.path.exists() else {
                'version': 1, 'members': {}, 'budget': {}, 'next_refresh': 0}
            if (self.state.get('version') != 1 or not isinstance(self.state.get('members'), dict) or
                    not isinstance(self.state.get('budget'), dict)):
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            raise ToolError('渠道维护状态无效，请检查保存记录。', 'mt_config') from None

    def _save(self):
        atomic_json(self.path, self.state)

    def _backup(self):
        folder = self.config.data / 'gateway-backups' / ('maintenance-' + str(time.time_ns()))
        folder.mkdir(parents=True)
        atomic_json(folder / 'providers.json', self.trial._registry())
        for path in (self.path, self.config.data / 'mt-channels.json'):
            if path.exists():
                (folder / path.name).write_bytes(path.read_bytes())
        self.state['last_backup'] = str(folder)

    def refresh_provider_lists(self, policy):
        providers = read_registry(self.config.settings, policy=policy)['providers']
        eligible = [p for p in providers if p['id'] in policy['providers'] and not p.get('off')
                    and p['id'] not in policy.get('excluded_providers', [])
                    and not p.get('noModels') and not p.get('listOff')]
        if eligible:
            self._backup()
        for provider in eligible:
            self.control.check_start()
            try:
                self.trial._command('provider', 'refresh', provider['id'])
            except Cancelled:
                raise
            except ToolError:
                self.state.setdefault('catalog_refresh', {})[provider['id']] = 'failed'
            else:
                self.state.setdefault('catalog_refresh', {})[provider['id']] = 'ok'
                fresh = next((p for p in read_registry(self.config.settings, policy=policy)['providers'] if p['id'] == provider['id']), {})
                for name in set(provider.get('models', [])) - set(fresh.get('models', [])):
                    member = provider['id'] + '/' + name
                    if self.state.get('exposed', {}).get(member) == 'managed':
                        self.state['exposed'][member] = 'catalog_removed'

    def expose(self, member):
        """Incrementally expose one catalog-backed candidate before its admission."""
        check_provider_policy(self.config, member)
        owner, name = member.split('/', 1)
        with self.trial.lock:
            self.control.check_start()
            self._backup()
            self.trial._command('provider', 'models', owner, '+' + name)
            provider = next(p for p in self.trial._registry()['providers'] if p['id'] == owner)
            if name not in provider.get('models', []):
                raise ToolError('候选未能加入 Magpie 型号列表。', 'magpie_group')
            self.state.setdefault('exposed', {})[member] = 'managed'
            self._save()

    def reserve_evaluation(self, policy):
        day = datetime.now(timezone.utc).date().isoformat()
        budget = self.state['budget']
        if budget.get('day') != day:
            budget = self.state['budget'] = {'day': day, 'evaluations': 0}
        if budget['evaluations'] >= policy['max_evaluations_per_day']:
            return False
        budget['evaluations'] += 1
        self._save()  # Reserve before inference; crash/restart never refunds it.
        return True

    def prepare(self, settings, base_url):
        from .magpie import bound_gateway
        if not bound_gateway(settings, base_url):
            return
        effective = self.config.mt_settings()
        for role, suffix in [('translate', ''), ('review', '_review'), ('judge', '_judge')]:
            name = 'group/transcribe' + ('' if role == 'translate' else '-' + role)
            if (getattr(effective, 'mt' + suffix + '_model') or name) != name or (
                    getattr(effective, 'mt' + suffix + '_fallback_model') or name + '-paid') != name + '-paid':
                return
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='mt-channels', daemon=True)
                self.thread.start()

    def close(self):
        self.stop.set()
        if self.owns_trial:
            self.trial.stop.set()
        if self.thread:
            self.thread.join()
        if self.owns_trial:
            self.trial.close()

    def _run(self):
        while not self.control.stopping:
            try:
                policy = load_policy(self.config)
                if policy['enabled'] and time.time() >= self.state.get('next_refresh', 0):
                    self.refresh(policy=policy)
            except Cancelled:
                return
            except Exception:
                # Never turn maintenance failure into a user transcription failure.
                self.state.update(last_status='refresh_failed', next_refresh=time.time() + 300)
                try:
                    self._save()
                except OSError:
                    pass
            self.stop.wait(5)

    def status(self):
        policy = load_policy(self.config)
        return {'enabled': policy['enabled'], 'providers': list(policy['providers']),
            'excluded_providers': policy['excluded_providers'],
            'refresh_seconds': policy['refresh_seconds'], 'budget': self.state['budget'],
            'max_evaluations_per_day': policy['max_evaluations_per_day'],
            'last_status': self.state.get('last_status', 'not_run'),
            'application_error': self.state.get('application_error'),
            'catalog_refresh': self.state.get('catalog_refresh', {}),
            'last_refresh': self.state.get('last_refresh'), 'next_refresh': self.state.get('next_refresh'),
            'members': {m: {k: r[k] for k in ('status', 'retry_after', 'billing') if k in r}
                        for m, r in self.state['members'].items()}}

    def _cached(self, setup, member, policy):
        # Search only independently verifiable keys, never accept a model name
        # match from a different account, interface or dynamic backend set.
        judges = list(policy['judges'])
        judges.extend(m for m, (role, _) in setup.current_members().items() if role in {'review', 'judge'})
        for judge in dict.fromkeys(judges):
            if judge.split('/', 1)[0] in policy.get('excluded_providers', []):
                continue
            try:
                if model_set(member, setup.catalog['identities']) & model_set(judge, setup.catalog['identities']):
                    continue
                key, result = setup.cached_evaluation(member, judge)
                if result is not None:
                    return key, result, judge
            except (KeyError, ValueError, StopIteration):
                continue
        return None, None, None

    def refresh(self, *, policy=None, discover=None, setup=None):
        self.control.check_start()
        policy = policy or load_policy(self.config)
        if not policy['enabled']:
            return self.status()
        setup = setup or GatewaySetup(self.config, self.control, manager=self.trial)
        before_catalog = copy.deepcopy(setup.catalog)
        before_registry = read_registry(self.config.settings, before_catalog, policy=policy)
        current = {m: r for m, r in setup.current_members().items()
                   if not m.startswith('transcribe-bailian-trial') and m.split('/', 1)[0] not in policy['excluded_providers']}
        records = self.state['members']
        previous_providers = {p['id']: p for p in before_registry['providers']}
        for member, state in list(self.state.get('exposed', {}).items()):
            owner, name = member.split('/', 1)
            if state == 'managed' and name not in previous_providers.get(owner, {}).get('models', []):
                self.state['exposed'][member] = 'manual_hold'
        # Bootstrap only from exact existing admission evidence, without calls.
        for member in current:
            record = records.setdefault(member, {})
            proof = identity_key(member, before_registry, before_catalog)
            if record.get('proof') == proof and record.get('accepted'):
                continue
            _, result, _ = self._cached(setup, member, policy)
            if result and result.get('roles'):
                record.update(proof=proof, accepted=copy.deepcopy(result))
        if discover is None:
            self.refresh_provider_lists(policy)
        catalog = discover or Catalog(self.config, self.control)
        discovered = catalog.discover(policy=policy, deprioritize={m for m, r in records.items()
            if r.get('status') in {'rejected', 'price_unknown', 'price_limit'}})
        for owner, metadata in discovered['models'].items():
            setup.catalog['models'][owner] = metadata
        for member, row in discovered['identities'].items():
            old = setup.catalog['identities'].get(member, {})
            if old.get('models') == row['models'] and old.get('admitted'):
                row = {**row, 'admitted': old['admitted']}
            setup.catalog['identities'][member] = row
        registry = read_registry(self.config.settings, setup.catalog, policy=policy)
        providers = {p['id']: p for p in registry['providers']}
        desired, free, calls = {}, [], 0
        qualified = {m for m, r in records.items() if r.get('accepted') and m.split('/', 1)[0] in policy['providers']}
        candidates = list(current) + sorted((set(discovered['identities']) | qualified) - set(current),
            key=lambda m: (records.get(m, {}).get('attempted_at', 0), m))
        for member in candidates:
            self.control.check_start()
            owner, name = member.split('/', 1)
            provider = providers.get(owner)
            record = records.setdefault(member, {})
            if owner in policy['excluded_providers']:
                record['status'] = 'provider_excluded'
                continue
            rule = policy['providers'].get(owner)
            proof = identity_key(member, registry, setup.catalog)
            accepted = record.get('accepted') if record.get('proof') == proof else None
            if provider is None or provider.get('off') or provider.get('noModels') or name in provider.get('modelsOff', []):
                record['status'] = 'disabled_or_missing'
                continue
            if self.state.get('exposed', {}).get(member) == 'manual_hold':
                record['status'] = 'manual_hold'
                continue
            if manually_held(member, current, setup, registry):
                record['status'] = 'manual_hold'
                continue
            if rule and owner in catalog.complete and member not in catalog.available and member not in catalog.pending:
                record['status'] = 'unavailable'
                continue
            billing = offer_billing(provider, name)
            record['billing'] = billing
            reason = candidate_allowed(name, billing, provider.get('_models', {}).get(name, {}), rule) if rule else 'not_enrolled'
            # Existing paid qualifications remain usable even without a published
            # price. Exclusions and a free -> paid transition do not get that waiver.
            if reason and not (accepted and reason in {'price_unknown', 'price_limit', 'not_enrolled'}):
                record['status'] = reason
                continue
            if rule and owner not in catalog.complete:
                record['status'] = 'catalog_unavailable'
            elif accepted:
                record['status'] = 'qualified'
            else:
                key, result, judge = self._cached(setup, member, policy)
                if result is None or result.get('retry_after') and result['retry_after'] <= time.time():
                    if record.get('attempt_proof') == proof and record.get('retry_after', 0) > time.time():
                        record['status'] = 'cooldown'
                        continue
                    if calls >= policy['max_evaluations_per_cycle']:
                        record['status'] = 'cycle_budget'
                        continue
                    try:
                        judge = judge or setup.independent_judge(member)
                    except (ToolError, ValueError):
                        record['status'] = 'no_independent_judge'
                        continue
                    if not self.reserve_evaluation(policy):
                        record['status'] = 'daily_budget'
                        continue
                    calls += 1
                    record.update(attempt_proof=proof, attempted_at=time.time(),
                                  retry_after=time.time() + policy['retry_seconds'], status='evaluating')
                    self._save()
                    if member in catalog.pending:
                        try:
                            self.expose(member)
                            catalog.pending.discard(member)
                        except Cancelled:
                            raise
                        except ToolError:
                            record['status'] = 'exposure_failed'
                            continue
                    result = setup.evaluate(member, judge)
                if result is None:
                    record['status'] = 'disabled_or_missing'
                    continue
                record['status'] = 'cooldown' if result.get('retry_after') else 'qualified' if result.get('roles') else 'rejected'
                accepted = retained_result(record.get('accepted'), result, same_identity=record.get('proof') == proof)
                if accepted.get('roles'):
                    record.update(accepted=copy.deepcopy(accepted), proof=proof)
            if accepted and accepted.get('roles'):
                if member in catalog.pending:
                    try:
                        self.expose(member)
                    except Cancelled:
                        raise
                    except ToolError:
                        record['status'] = 'exposure_failed'
                        continue
                    catalog.pending.discard(member)
                desired[member] = accepted
                if billing == 'free':
                    free.append(member)
        self.state.update(last_refresh=time.time(), next_refresh=time.time() + policy['refresh_seconds'],
                          last_status='checked', skipped=catalog.skipped)
        self._save()
        # Discovery is not a routing change. Publish metadata only for qualified
        # members so an unaccepted new model cannot churn policy/cache versions.
        routing_catalog = copy.deepcopy(before_catalog)
        for member in desired:
            owner, name = member.split('/', 1)
            if member in setup.catalog['identities']:
                routing_catalog['identities'][member] = setup.catalog['identities'][member]
            metadata = setup.catalog['models'].get(owner, {})
            if name in metadata:
                routing_catalog['models'].setdefault(owner, {})[name] = metadata[name]
        setup.catalog = routing_catalog
        # Trial discoveries may finish while cloud admission runs. Re-read their
        # reservations under the same lock that protects trial group mutations.
        with self.trial.lock:
            self.control.check_start()
            if load_policy(self.config) != policy:
                self.state['last_status'] = 'policy_changed'
            else:
                try:
                    applied = setup.apply(desired, free)
                    self.state['last_status'] = 'updated' if applied['changed'] else 'unchanged'
                    self.state.pop('application_error', None)
                    self.state['last_application'] = applied
                except Cancelled:
                    raise
                except ToolError as error:
                    self.state['last_status'] = 'application_blocked'
                    self.state['application_error'] = error.code
        self._save()
        return self.status()

"""Read the configured subtitle routes without starting Magpie or accessing credentials."""
from __future__ import annotations

import json
from collections import Counter

from .bailian_state import TrialState
from .common import ToolError
from .magpie import bound_gateway, group_model_paths, managed_gateway
from .mt_catalog import classify_offer, read_registry, saved_catalog
from .mt_diagnostics import safe_model_id
from .mt_identity import dynamic_model, model_set


def _safe_id(value):
    if not safe_model_id(value):
        raise ValueError()
    return value


def _entry_status(registry, endpoint, identities, owned, trial_members, excluded):
    members = {}
    blocked = False
    for path, owner, name, provider in group_model_paths(registry, endpoint):
        member = _safe_id(owner + '/' + name)
        if owner in excluded:
            blocked = True
        if member not in members:
            identity = identities.get(member, {})
            declared = model_set(member, identities)
            admitted = declared
            if dynamic_model(member) or identity.get('dynamic'):
                admitted = declared.intersection(identity.get('admitted', []))
            billing, basis = classify_offer(provider, name)
            if identity.get('billing') and identity['billing'] != billing:
                billing, basis = identity['billing'], 'saved_catalog'
            if billing not in {'free', 'trial', 'commercial', 'unknown'}:
                raise ValueError()
            row = {'member': member, 'models': sorted(_safe_id(model) for model in admitted),
                   'project_billing': billing, 'billing_basis': basis,
                   'magpie_count_free': provider.get('countFree') is True, 'paths': []}
            if billing == 'trial':
                row['trial_status'] = safe_model_id(trial_members.get(member, {}).get('status')) or 'unknown'
            members[member] = row
        parent = path[-2].removeprefix('group/') if len(path) > 1 else None
        trial = trial_members.get(member, {})
        trial_role = trial.get('role')
        trial_group = 'transcribe-trial' if trial_role == 'translate' else 'transcribe-trial-' + str(trial_role)
        if trial.get('joined') and parent == trial_group:
            owner_type = 'trial'
        elif path[-1] in owned.get(parent, ()):
            owner_type = 'project'
        else:
            owner_type = 'manual'
        members[member]['paths'].append({'groups': [_safe_id(g) for g in path[:-1]], 'member_owner': owner_type})
    rows = list(members.values())
    return {'status': 'excluded_provider' if blocked else 'configured' if rows else 'empty',
            'member_count': len(rows), 'channel_count': len({m.split('/', 1)[0] for m in members}),
            'model_count': len({model for row in rows for model in row['models']}),
            'billing_counts': dict(Counter(row['project_billing'] for row in rows)), 'members': rows}


def role_status(config, policy):
    settings = config.mt_settings()
    roles = {role: {kind: {'entry': safe_model_id(model), 'status': 'unbound'}
                    for kind, model in zip(('primary', 'fallback'), pair)}
             for role, pair in settings.role_models().items()}
    result = {'bound': False, 'managed': False, 'policy_version': settings.mt_policy_version,
              'mt_workers': settings.mt_workers, 'roles': roles, 'warnings': []}
    if not bound_gateway(settings, settings.mt_base_url):
        return result
    result.update(bound=True, managed=managed_gateway(config, settings.mt_base_url))
    try:
        catalog = saved_catalog(config)
        registry = read_registry(settings, catalog, policy=policy, fingerprint_accounts=False)
        managed_path = config.data / 'gateway-managed.json'
        owned = json.loads(managed_path.read_bytes())['groups'] if managed_path.exists() else {}
        try:
            trial = TrialState(config).read()
        except ToolError:
            trial = {}
            result['warnings'].append('trial_state_unavailable')
        result['trial'] = {'quota_status': safe_model_id(trial.get('quota_status')) or 'unknown',
                           'paused': trial.get('paused')}
        trial_members = {row['provider'] + '/' + model: row for model, row in trial.get('models', {}).items()
                         if isinstance(row.get('provider'), str)}
        for endpoints in roles.values():
            for entry in endpoints.values():
                try:
                    entry.update(_entry_status(registry, entry['entry'], catalog.get('identities', {}),
                                               owned, trial_members, policy['excluded_providers']))
                except (ValueError, KeyError, TypeError, AttributeError):
                    entry['status'] = 'unverified'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ToolError):
        result['warnings'].append('registry_unavailable')
        for endpoints in roles.values():
            for entry in endpoints.values():
                entry['status'] = 'unverified'
    return result

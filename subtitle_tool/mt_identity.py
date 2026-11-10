"""Model identity, separate from the gateway member used for billing."""
from __future__ import annotations

import re


# Verified aliases in the Zen, Vercel and OpenRouter catalogs. Do not strip
# arbitrary '-free' suffixes: a different product is not necessarily an alias.
FREE_ALIASES = {name + '-free': name for name in (
    'deepseek-v4.1-flash', 'glm-5.3-flash', 'mimo-v2.6-flash', 'longcat-2.5',
    'beam', 'flint-40b', 'nemotron-3.5-super-vl', 'nemotron-3.5-lightning', 'minimax-m3.1-flash')}
VSR_MEMBER = 'vllm-sr/vllm-sr/auto'


def canonical_model(model):
    model = re.sub(r'(?::(?:none|minimal|low|medium|high|xhigh|max|free))+$', '', model.lower())
    model = model.rsplit('/', 1)[-1].replace('_', '-')
    model = FREE_ALIASES.get(model, model)
    return re.sub(r'-(?:20\d{2}-\d{2}-\d{2}|\d{4})$', '', model)


def dynamic_model(member):
    return member.rsplit('/', 1)[-1].lower() in {'auto', 'free', 'big-pickle', 'iceman-free'}


def model_set(member, identities=None):
    row = (identities or {}).get(member)
    if row is not None:
        return set(row['models'])
    if dynamic_model(member):
        raise ValueError('Unverified routed model')
    return {canonical_model(member)}


def resolve_model(member, headers, identities=None, *, saved=None, require_admitted=False):
    """An anonymous router never supplies its own independence proof."""
    from .mt_diagnostics import safe_model_id
    if not safe_model_id(member) or member.startswith('group/'):
        raise ValueError('Missing member identity')
    row = (identities or {}).get(member)
    if dynamic_model(member) or row and row['dynamic']:
        if member != VSR_MEMBER or not row or not row['dynamic']:
            raise ValueError('Unverified routed model')
        raw = saved if saved is not None else headers.get('x-vsr-selected-model')
        if not safe_model_id(raw):
            raise ValueError('Missing underlying model')
        resolved = canonical_model(raw)
        allowed = row.get('admitted', [] if require_admitted else row['models'])
        if dynamic_model(raw) or resolved not in row['models'] or resolved not in allowed:
            raise ValueError('Unadmitted underlying model')
        return resolved
    resolved = canonical_model(member)
    if saved is not None and saved != resolved or row and resolved not in row['models']:
        raise ValueError('Changed underlying identity')
    return resolved


def valid_identity_map(value):
    from .mt_diagnostics import safe_model_id
    def valid(member, row):
        return (
        isinstance(member, str) and safe_model_id(member) == member and not member.startswith('group/') and
        isinstance(row, dict) and set(row) - {'admitted'} == {'models', 'dynamic', 'billing'} and
        type(row['dynamic']) is bool and isinstance(row['billing'], str) and
        row['billing'] in {'free', 'trial', 'commercial', 'unknown'} and
        isinstance(row['models'], list) and 0 < len(row['models']) <= 32 and
        all(isinstance(m, str) and safe_model_id(m) == m and canonical_model(m) == m and
            not dynamic_model(m) for m in row['models']) and
        row['models'] == sorted(set(row['models'])) and
        ('admitted' not in row or isinstance(row['admitted'], list) and
         all(isinstance(m, str) and m in row['models'] for m in row['admitted']) and
         row['admitted'] == sorted(set(row['admitted']))) and
        (member == VSR_MEMBER if row['dynamic'] else
         not dynamic_model(member) and row['models'] == [canonical_model(member)]))
    return isinstance(value, dict) and len(value) <= 2048 and all(valid(k, v) for k, v in value.items())

"""Explicit provider allowlist; adding a provider never requires editing Python."""
from __future__ import annotations

import json
import math
import re
from fnmatch import fnmatchcase
from pathlib import Path

from .common import ToolError


def default_policy():
    return json.loads(Path(__file__).with_name('mt-channels.default.json').read_bytes())


def load_policy(config):
    path = config.data / 'mt-channels.json'
    try:
        value = json.loads(path.read_bytes()) if path.exists() else default_policy()
        if isinstance(value, dict):
            value.setdefault('excluded_providers', [])
        if (not isinstance(value, dict) or set(value) != set(default_policy()) or value['version'] != 1 or
                type(value['enabled']) is not bool or not isinstance(value['providers'], dict) or
                not isinstance(value['judges'], list) or not all(isinstance(m, str) and '/' in m for m in value['judges'])):
            raise ValueError()
        excluded = value['excluded_providers']
        if not isinstance(excluded, list) or not all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in excluded):
            raise ValueError()
        if set(excluded) & set(value['providers']) or any(m.split('/', 1)[0] in excluded for m in value['judges']):
            raise ToolError('渠道已明确排除，不能登记或用于验收评判；请检查 excluded_providers。', 'mt_provider_excluded')
        for field, minimum, maximum in [('refresh_seconds', 60, 86400), ('retry_seconds', 300, 604800),
                ('max_evaluations_per_cycle', 0, 20), ('max_evaluations_per_day', 0, 100)]:
            if type(value[field]) is not int or not minimum <= value[field] <= maximum:
                raise ValueError()
        fields = {'adapter', 'package', 'preferred', 'include', 'exclude', 'billing', 'max_candidates',
                  'max_input_usd_per_million', 'max_output_usd_per_million'}
        required = {'adapter', 'preferred', 'include', 'exclude', 'billing', 'max_candidates'}
        for owner, rule in value['providers'].items():
            if (not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or not isinstance(rule, dict) or
                    set(rule) - fields or not required <= set(rule) or
                    rule['adapter'] not in {'registry', 'openrouter', 'vercel', 'vllm-sr'}):
                raise ValueError()
            for field in ('preferred', 'include', 'exclude', 'billing'):
                if not isinstance(rule[field], list) or not all(isinstance(m, str) and m for m in rule[field]):
                    raise ValueError()
            if set(rule['billing']) - {'free', 'commercial'} or set(rule['max_candidates']) != {'free', 'commercial'}:
                raise ValueError()
            if any(type(n) is not int or not 0 <= n <= 100 for n in rule['max_candidates'].values()):
                raise ValueError()
            if 'package' in rule and (not isinstance(rule['package'], str) or not re.fullmatch(
                    r'(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+', rule['package']) or '..' in rule['package']):
                raise ValueError()
            for field in ('max_input_usd_per_million', 'max_output_usd_per_million'):
                if field in rule and (type(rule[field]) not in (int, float) or
                        not math.isfinite(rule[field]) or not 0 <= rule[field] <= 1000):
                    raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ToolError('渠道维护配置无效，请检查 data/mt-channels.json。', 'mt_config') from None


def selected(name, rule):
    return any(fnmatchcase(name, p) for p in rule['include']) and not any(
        fnmatchcase(name, p) for p in rule['exclude'])


def check_provider_policy(config, models, *, policy=None, registry=None, base_url=None):
    """Refuse excluded accounts before any inference, including nested groups."""
    policy = policy or load_policy(config)
    excluded = set(policy.get('excluded_providers', []))
    if not excluded:
        return
    from .magpie import bound_gateway, group_models
    from .mt_catalog import read_registry
    for model in [models] if isinstance(models, str) else models:
        owner = model.split('/', 1)[0]
        owners = {owner}
        if owner == 'group' and bound_gateway(config.settings, base_url or config.settings.mt_base_url):
            try:
                if registry is None:
                    registry = read_registry(config.settings, policy=policy)
                owners = {p for p, _, _ in group_models(registry, model)}
            except (OSError, ValueError, KeyError, TypeError):
                raise ToolError('无法核对字幕组的渠道排除规则。', 'magpie_group') from None
        if owners & excluded:
            raise ToolError('请求涉及已明确排除的渠道，未发送推理请求。', 'mt_provider_excluded')


def candidate_allowed(name, billing, row, rule):
    if billing not in rule['billing']:
        return 'billing_excluded'
    if not selected(name, rule):
        return 'model_excluded'
    if billing == 'free' or name in rule['preferred']:
        return None  # These specific commercial offers were explicitly selected.
    if rule.get('adapter') != 'vercel':
        return 'price_unknown'
    # Only Vercel's official per-token pricing has a defined unit here. A plugin's
    # generic `cost` may mean cash, credits or subscription multipliers.
    try:
        price = row['pricing']
        costs = [float(price[k]) * 1000000 for k in ('input', 'output')]
        if not all(math.isfinite(n) and n >= 0 for n in costs):
            return 'price_unknown'
        if set(price) - {'input', 'output'}:
            return 'price_unknown'
        ceilings = [rule.get('max_input_usd_per_million', 2), rule.get('max_output_usd_per_million', 10)]
        return None if all(a <= b for a, b in zip(costs, ceilings)) else 'price_limit'
    except (KeyError, ValueError, TypeError):
        return 'price_unknown'

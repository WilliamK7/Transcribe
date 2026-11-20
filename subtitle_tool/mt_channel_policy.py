"""Provider selection, evaluation budgets and price permissions."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from .common import ToolError

ROLES = ('translate', 'review', 'judge')


def supplier_routing(role):
    # Reviews gate completion of a group. Let Magpie use recent streamed
    # latency within a billing tier; unknown suppliers still get probes.
    return 'weight' if role == 'translate' else 'fast'


def default_policy():
    return json.loads(Path(__file__).with_name('mt-channels.default.json').read_bytes())


def load_policy(config):
    path = config.data / 'mt-channels.json'
    try:
        defaults = default_policy()
        value = json.loads(path.read_bytes()) if path.exists() else defaults
        if isinstance(value, dict):
            value.setdefault('excluded_providers', [])
        if (not isinstance(value, dict) or value.keys() != defaults.keys() or type(value['version']) is not int or
                value['version'] != 2 or
                type(value['enabled']) is not bool or not isinstance(value['providers'], dict) or
                not isinstance(value['judges'], list) or not all(isinstance(m, str) and '/' in m for m in value['judges'])):
            raise ValueError()
        excluded = value['excluded_providers']
        if not isinstance(excluded, list) or not all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in excluded):
            raise ValueError()
        for field, minimum, maximum in [('refresh_seconds', 60, 86400), ('retry_seconds', 300, 604800),
                ('max_evaluations_per_cycle', 0, 20), ('max_evaluations_per_day', 0, 100)]:
            if type(value[field]) is not int or not minimum <= value[field] <= maximum:
                raise ValueError()
        fields = {'adapter', 'package', 'preferred', 'price_exceptions', 'include', 'exclude', 'billing', 'max_candidates',
                  'max_input_usd_per_million', 'max_output_usd_per_million'}
        required = {'adapter', 'billing', 'max_candidates'}
        for owner, rule in value['providers'].items():
            if (not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or not isinstance(rule, dict) or
                    set(rule) - fields or not required <= set(rule) or
                    rule['adapter'] not in {'registry', 'openrouter', 'vercel', 'vllm-sr'}):
                raise ValueError()
            for field in rule.keys() & {'preferred', 'price_exceptions', 'include', 'exclude', 'billing'}:
                if not isinstance(rule[field], list) or not all(isinstance(m, str) and m for m in rule[field]):
                    raise ValueError()
            tiers = set(rule['billing'])
            limits = rule['max_candidates']
            if (not isinstance(limits, dict) or tiers - {'free', 'commercial'} or
                    not tiers <= limits.keys() <= {'free', 'commercial'}):
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
    return any(fnmatchcase(name, p) for p in rule.get('include', ('*',))) and not any(
        fnmatchcase(name, p) for p in rule.get('exclude', ()))


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


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str


def candidate_admission(name, billing, row, rule, *, qualified=False):
    """Only a verified past qualification may be retained without a new paid evaluation."""
    def price_block(reason):
        return AdmissionDecision(True, 'existing_qualification') if qualified else AdmissionDecision(False, reason)

    if rule is None:
        return price_block('not_enrolled')
    if billing not in rule['billing']:
        return AdmissionDecision(False, 'billing_excluded')
    if not selected(name, rule):
        return AdmissionDecision(False, 'model_excluded')
    if billing == 'free':
        return AdmissionDecision(True, 'free_offer')
    if name in rule.get('price_exceptions', ()):
        return AdmissionDecision(True, 'price_exception')
    if rule.get('adapter') != 'vercel':
        return price_block('price_unknown')
    # Only Vercel's official per-token pricing has a defined unit here. A plugin's
    # generic `cost` may mean cash, credits or subscription multipliers.
    try:
        price = row['pricing']
        costs = [float(price[k]) * 1000000 for k in ('input', 'output')]
        if not all(math.isfinite(n) and n >= 0 for n in costs):
            return price_block('price_unknown')
        if set(price) - {'input', 'output'}:
            return price_block('price_unknown')
        ceilings = [rule.get('max_input_usd_per_million', 2), rule.get('max_output_usd_per_million', 10)]
        if all(a <= b for a, b in zip(costs, ceilings)):
            return AdmissionDecision(True, 'within_price_limit')
        return price_block('price_limit')
    except (KeyError, ValueError, TypeError):
        return price_block('price_unknown')

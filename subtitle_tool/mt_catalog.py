"""Read-only gateway catalog; plugin credentials stay in Magpie."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .common import ToolError
from .mt_identity import VSR_MEMBER, canonical_model, dynamic_model
from .mt_channel_policy import default_policy, load_policy, selected


PLUGIN_PACKAGES = {p: r['package'] for p, r in default_policy()['providers'].items() if 'package' in r}


def _read(path, default):
    return json.loads(path.read_bytes()) if path.is_file() else default


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_registry(settings, catalog=None, *, policy=None):
    folder = Path(settings.magpie_data_dir)
    raw = _read(folder / 'providers.json', {'providers': [], 'groups': []})
    providers = {p['id']: dict(p) for p in raw['providers']}
    # Auth is read only to hash identity; never include its fields in the view.
    auth = _read(folder / 'plugin-auth.json', {})
    for plugin in _read(folder / 'plugin-providers.json', []):
        owner = plugin['id']
        override = providers.get(owner, {})
        metadata = {m['id']: m for m in plugin.get('models', [])}
        credentials = {k: v for k, v in auth.items() if k == owner or k.startswith(owner + '#')}
        spec = plugin.get('spec', '')
        match = re.fullmatch(r'((?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)(?:@[^/]+)?', spec)
        configured_package = (policy or {}).get('providers', {}).get(owner, {}).get('package')
        package_name = configured_package or (match[1] if match and '..' not in match[1] else PLUGIN_PACKAGES.get(owner, '__unknown__'))
        package = _read(folder / 'plugins' / 'node_modules' / package_name /
                        'package.json', {})
        fingerprint = {'spec': plugin.get('spec'), 'version': package.get('version'),
            'npm': plugin.get('npm'), 'api': plugin.get('api'), 'accounts': plugin.get('accounts'), 'auth': credentials}
        providers[owner] = {'id': owner, 'chat': plugin.get('api', ''),
            'models': list(metadata), 'maxConcurrency': plugin.get('maxConcurrency'), **override,
            'off': bool(override.get('off')) or plugin.get('signedIn') is False,
            '_models': metadata, '_plugin': plugin.get('spec'),
            '_plugin_fingerprint': _fingerprint(fingerprint)}
    for owner, metadata in (catalog or {}).get('models', {}).items():
        if owner in providers:
            providers[owner]['_models'] = {**metadata, **providers[owner].get('_models', {})}
    return {**raw, 'providers': list(providers.values())}


def zero_priced(pricing):
    try:
        return (isinstance(pricing, dict) and all(k in pricing for k in ('input', 'output')) and
                all(Decimal(str(value)) == 0 for key, value in pricing.items()
                    if key not in ('input_tiers', 'output_tiers')) and
                all(Decimal(str(t['cost'])) == 0 for k in ('input_tiers', 'output_tiers')
                    for t in pricing.get(k, [])))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return False


def offer_billing(provider, name):
    from .magpie import known_free_model
    if provider['id'] == 'transcribe-bailian-trial' or provider['id'].startswith('transcribe-bailian-trial-'):
        return 'trial'
    row = provider.get('_models', {}).get(name, {})
    if provider['id'] == 'vercel':
        return 'free' if zero_priced(row.get('pricing')) else 'commercial'
    if provider['id'] in {'github-copilot', 'cloudflare-workers-ai'}:
        return 'commercial'
    if provider.get('_plugin') or provider['id'] in PLUGIN_PACKAGES:
        return 'free' if row.get('free') is True else 'commercial'
    if type(row.get('free')) is bool:
        return 'free' if row['free'] else 'commercial'
    if known_free_model(provider.get('chat', '').rstrip('/'), name):
        return 'free'
    return 'commercial'


def saved_catalog(config):
    data = _read(config.data / 'gateway-catalog.json', {})
    if data and (not isinstance(data, dict) or data.get('version') != 1):
        raise ToolError('渠道目录版本无效，请重新验收配置。', 'mt_config')
    return data


class Catalog:
    def __init__(self, config, control, transport=None):
        from .mt_transport import ProcessTransport
        self.config, self.control = config, control
        self.transport = transport or ProcessTransport()
        self.data = {'version': 1, 'models': {}, 'identities': {}}
        self.skipped = []
        self.available = set()
        self.complete = set()
        self.pending = set()

    def get(self, url, *, local=False):
        headers = {'Authorization': 'Bearer ' + self.config.get_magpie_key()} if local else {}
        response = self.transport.request('GET', url, headers=headers, control=self.control, timeout=20, drain=False)
        if response.status != 200:
            raise ToolError('无法读取渠道目录。', 'mt_catalog')
        return json.loads(response.body)

    def discover(self, providers=None, *, policy=None, deprioritize=()):
        from .mt_diagnostics import safe_model_id
        policy = policy or load_policy(self.config)
        providers = tuple(policy['providers']) if providers is None else providers
        if set(providers) - set(policy['providers']):
            raise ToolError('请先在 data/mt-channels.json 中指定要管理的渠道。', 'mt_config')
        raw = read_registry(self.config.settings, policy=policy)
        by_id = {p['id']: p for p in raw['providers']}
        # Only the gateway's actual member IDs are used. No guessed prefix stripping.
        served = self.get(self.config.settings.mt_base_url + '/models', local=True)
        available = {r['id'] for r in served.get('data', [])}
        self.available = available
        if served.get('has_more'):
            raise ToolError('网关模型目录未完整返回，不能据此修改分组。', 'mt_catalog')
        vsr_models = []
        for owner in providers:
            if owner in policy.get('excluded_providers', []):
                self.skipped.append({'provider': owner, 'reason': 'provider_excluded'})
                continue
            rule = policy['providers'][owner]
            provider = by_id.get(owner)
            if not provider or provider.get('off') or provider.get('noModels'):
                self.skipped.append({'provider': owner, 'reason': 'disabled_or_missing'})
                self.complete.add(owner)
                continue
            try:
                metadata = provider.get('_models', {})
                if rule['adapter'] == 'vercel':
                    rows = self.get('https://ai-gateway.vercel.sh/v1/models')['data']
                    metadata = {r['id']: {k: r[k] for k in ('id', 'type', 'modalities', 'pricing',
                                'supported_parameters', 'reasoning_options') if k in r} for r in rows}
                elif rule['adapter'] == 'openrouter':
                    rows = self.get('https://openrouter.ai/api/v1/models')['data']
                    metadata = {r['id']: {'id': r['id'], 'modalities': {
                        'output': r.get('architecture', {}).get('output_modalities', [])},
                        'free': r['id'].endswith(':free') and r.get('pricing', {}).get('prompt') in ('0', '0.0')
                            and r.get('pricing', {}).get('completion') in ('0', '0.0')}
                        for r in rows}
                elif rule['adapter'] == 'vllm-sr':
                    info = self.get('https://app.vllm-sr.ai/pt/api/info')
                    vsr_models = self._routes_to(info)
                    if not vsr_models:
                        raise ToolError('动态模型目录缺少后端集合。', 'mt_catalog')
                self.data['models'][owner] = metadata
                self.complete.add(owner)
                listed = set(metadata)
                # Magpie's fetched vendor list is broader than its exposed picks.
                # Bind the local cache to the configured endpoint; never borrow a
                # stale list left by a different endpoint with the same provider ID.
                if not provider.get('_plugin') and not provider.get('listOff'):
                    live = _read(Path(self.config.settings.magpie_data_dir) / 'cache' / 'models' / (owner + '.json'), {})
                    if live.get('base') and live['base'] in [provider.get(k) for k in ('chat', 'responses', 'anthropic')]:
                        listed = {r['ID'] for r in live.get('models', []) if isinstance(r.get('ID'), str)}
                    elif rule['adapter'] != 'openrouter':
                        listed = set()
                # Vercel's public list alone is not account availability evidence.
                if rule['adapter'] == 'vercel':
                    listed &= set(metadata)
                counts = {'free': 0, 'commercial': 0}
                names = list(dict.fromkeys(rule['preferred'] + sorted(m[len(owner) + 1:]
                             for m in available if m.startswith(owner + '/')) + sorted(listed)))
                names.sort(key=lambda n: owner + '/' + n in deprioritize)
                for name in names:
                    member = owner + '/' + name
                    row = metadata.get(name, {})
                    outputs = row.get('outputs', row.get('modalities', {}).get('output'))
                    if (safe_model_id(member) != member or member not in available and name not in listed or
                            name in provider.get('modelsOff', []) or row.get('decides') or
                            outputs is not None and 'text' not in outputs or row.get('type', 'language') != 'language' or
                            dynamic_model(member) and member != VSR_MEMBER or not selected(name, rule)):
                        self.skipped.append({'model': member, 'reason': 'not_available'})
                        continue
                    fee = offer_billing({**provider, '_models': metadata}, name)
                    if fee not in rule['billing'] or counts[fee] >= rule['max_candidates'][fee]:
                        continue
                    counts[fee] += 1
                    if member not in available:
                        self.pending.add(member)
                    self.data['identities'][member] = {'models': (vsr_models if member == VSR_MEMBER else
                        [canonical_model(member)]), 'dynamic': member == VSR_MEMBER,
                        'billing': fee}
            except (ToolError, ValueError, KeyError, TypeError):
                self.complete.discard(owner)
                self.skipped.append({'provider': owner, 'reason': 'catalog_unavailable'})
        return self.data

    @staticmethod
    def _routes_to(info):
        # Only the System Two auto route is a translation candidate. Never take
        # a decision model's separate backend set or silently drop unknown IDs.
        from .mt_diagnostics import safe_model_id
        if not isinstance(info, dict):
            return []
        found = info.get('routes_to')
        if found is not None and info.get('id', 'auto') in {'auto', 'vllm-sr/auto'}:
            if not isinstance(found, list) or not all(isinstance(v, str) and safe_model_id(v) == v and
                                                     not dynamic_model(v) for v in found):
                return []
            return sorted({canonical_model(v) for v in found})
        for value in info.values():
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, dict):
                    found = Catalog._routes_to(child)
                    if found:
                        return found
        return []

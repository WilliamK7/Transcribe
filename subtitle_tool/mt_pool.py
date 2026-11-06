"""Build and validate self-contained multi-provider translation route snapshots."""
from __future__ import annotations

import copy
import re
import unicodedata

from .common import ToolError
from .mt_backend import (MTBackend, OPENROUTER_BASE_URL, QINIU_BASE_URL, QINIU_FREE_MODEL,
                         STRUCTURED_BAILIAN_MODEL, bailian_mt_url, key_fingerprint,
                         normalize_mt_url, saved_mt)


# Defaults are a template for new tasks only. A saved route is validated from its
# own identities, never by comparing it with the current list or model names.
DEFAULT_ENTRIES = (
    ('qiniu_free', 'free', 3, 'qiniu', 'qiniu', QINIU_BASE_URL, QINIU_FREE_MODEL),
    ('openrouter_free', 'free', 1, 'openrouter', 'openai_compatible', OPENROUTER_BASE_URL, 'openrouter/free'),
    ('qiniu_paid', 'paid', 3, 'qiniu', 'qiniu', QINIU_BASE_URL, 'deepseek/deepseek-v4.1-flash'),
    ('bailian_paid', 'paid', 1, 'bailian', 'bailian', None, STRUCTURED_BAILIAN_MODEL),
)

# Explicit selections only; the normal pool template retains its original paid
# Bailian fallback. Profiles reflect the official structured-output support,
# while the smaller limits are the application's conservative v5 budgets.
BAILIAN_FREE_MODELS = {
    'qwen3.8-27b': ('bailian_free_qwen38_27b', 'json_object'),
    'qwen3.8-max-0902': ('bailian_free_qwen38_max_0902', 'json_schema'),
}

_PROVIDER_REFS = {'qiniu': 'qiniu', 'openai_compatible': 'openrouter', 'bailian': 'bailian'}
_FORMATS = {'json_object', 'json_schema', 'text'}
_ENTRY_FIELDS = {'id', 'tier', 'weight', 'rpm', 'credential_ref', 'backend', 'capabilities'}
_CONFIG_FIELDS = {'id', 'provider', 'base_url', 'model', 'credential_ref', 'tier', 'weight', 'rpm',
                  'enabled', 'capabilities'}
_BACKEND_FIELDS = {'format_version', 'provider', 'base_url', 'model', 'key_fingerprint', 'request_version'}
_CAPABILITY_FIELDS = {'response_format', 'input_bytes', 'output_tokens'}
_ROUTING_FIELDS = {'paid_enabled', 'free_wait_seconds', 'stage_deadline_seconds', 'http_timeout_seconds', 'max_attempts'}
_QUALITY_FIELDS = {'input_bytes', 'output_tokens', 'max_revisions', 'manual_glossary'}
_FORBIDDEN_UNICODE = {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}


def _invalid():
    return ToolError('翻译池配置或任务快照无效；请检查设置或用 --reflow。', 'mt_config')


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _fixed_integer(value, expected):
    return type(value) is int and value == expected


def _text(value, maximum):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and
            all(unicodedata.category(char) not in _FORBIDDEN_UNICODE for char in value))


def _model(value):
    return _text(value, 500) and value == value.strip() and not any(char.isspace() for char in value)


def _fingerprint(key):
    try:
        return key_fingerprint(key)
    except (TypeError, UnicodeError, ValueError):
        raise _invalid() from None


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value) is not None


def _manual_glossary(value):
    return (isinstance(value, dict) and len(value) <= 200 and
            all(_text(source, 200) and _text(target, 500) for source, target in value.items()))


def _capabilities(value):
    return (isinstance(value, dict) and set(value) == _CAPABILITY_FIELDS and
            isinstance(value['response_format'], str) and value['response_format'] in _FORMATS and
            _integer(value['input_bytes'], 1, 1048576) and _integer(value['output_tokens'], 1, 32768))


def _default_capabilities(provider):
    return {'response_format': 'json_object' if provider == 'qiniu' else 'json_schema',
            'input_bytes': 32768, 'output_tokens': 4096}


def _routing(value):
    return (isinstance(value, dict) and set(value) == _ROUTING_FIELDS and
            type(value['paid_enabled']) is bool and _integer(value['free_wait_seconds'], 0, 15) and
            _fixed_integer(value['stage_deadline_seconds'], 180) and
            _fixed_integer(value['http_timeout_seconds'], 60) and _fixed_integer(value['max_attempts'], 2))


def _quality(value):
    return (isinstance(value, dict) and set(value) == _QUALITY_FIELDS and
            _fixed_integer(value['input_bytes'], 32768) and _fixed_integer(value['output_tokens'], 4096) and
            _fixed_integer(value['max_revisions'], 2) and _manual_glossary(value['manual_glossary']))


def _official_url(provider, manifest):
    if provider == 'qiniu':
        return QINIU_BASE_URL
    if provider == 'openai_compatible':
        return OPENROUTER_BASE_URL
    if provider == 'bailian':
        return bailian_mt_url(manifest)
    raise _invalid()


def _base_url(provider, value, manifest):
    if provider == 'openai_compatible':
        return normalize_mt_url(value)
    expected = _official_url(provider, manifest)
    if provider == 'bailian' and value is None:
        return expected
    # An explicit address must resolve to the selected provider's fixed credential domain.
    if not isinstance(value, str) or normalize_mt_url(value) != expected:
        raise _invalid()
    return expected


def _defaults():
    templates = {}
    for identity, tier, weight, ref, provider, base, model in DEFAULT_ENTRIES:
        templates[identity] = {'id': identity, 'tier': tier, 'weight': weight, 'credential_ref': ref,
                               'provider': provider, 'base_url': base, 'model': model, 'enabled': True,
                               'rpm': None if identity == 'openrouter_free' else 60 if ref == 'bailian' else 0,
                               'capabilities': _default_capabilities(provider)}
    return templates


def _configured_specs(config, manifest):
    raw = config.settings.mt_backends
    templates = _defaults()
    # An explicit list defines the complete pool, in order. Existing template
    # IDs may be written as short overrides; omitted IDs are absent.
    if raw is None:
        selected = config.mt_settings()
        if selected.mt_provider == 'pool':
            raw = [{'id': identity} for identity in templates]
        elif selected.mt_provider == 'bailian':
            raw = [{'id': 'bailian_paid', 'model': selected.mt_model or STRUCTURED_BAILIAN_MODEL}]
        elif selected.mt_provider == 'qiniu':
            model = selected.mt_model or QINIU_FREE_MODEL
            raw = [{'id': 'qiniu_free', 'model': model, 'tier': 'free' if model == QINIU_FREE_MODEL else 'paid'}]
        elif selected.mt_provider == 'openai_compatible':
            if not _model(selected.mt_model):
                raise _invalid()
            base = normalize_mt_url(selected.mt_base_url)
            free = base == OPENROUTER_BASE_URL and (selected.mt_model == 'openrouter/free' or selected.mt_model.endswith(':free'))
            raw = [{'id': 'openrouter_free', 'base_url': base, 'model': selected.mt_model,
                    'tier': 'free' if free else 'paid', 'rpm': config.mt_rpm()}]
        else:
            raise _invalid()
    if not isinstance(raw, list):
        raise _invalid()
    result, seen_ids, seen_backends = [], set(), set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - _CONFIG_FIELDS:
            raise _invalid()
        identity = item.get('id')
        if not _identifier(identity) or identity in seen_ids:
            raise _invalid()
        seen_ids.add(identity)
        preset = templates.get(identity)
        spec = copy.deepcopy(preset) if preset is not None else {}
        if preset is not None and 'capabilities' in item:
            supplied = item['capabilities']
            if not isinstance(supplied, dict) or set(supplied) - _CAPABILITY_FIELDS:
                raise _invalid()
            spec['capabilities'].update(supplied)
            spec.update({key: value for key, value in item.items() if key != 'capabilities'})
        else:
            spec.update(copy.deepcopy(item))
        spec.setdefault('enabled', True)
        if set(spec) != _CONFIG_FIELDS:
            raise _invalid()
        provider, ref = spec['provider'], spec['credential_ref']
        if (not isinstance(provider, str) or provider not in _PROVIDER_REFS or
                not isinstance(ref, str) or _PROVIDER_REFS[provider] != ref or
                not isinstance(spec['tier'], str) or spec['tier'] not in {'free', 'paid'} or
                type(spec['enabled']) is not bool or not _integer(spec['weight'], 1, 100) or
                not _model(spec['model']) or not _capabilities(spec['capabilities'])):
            raise _invalid()
        if spec['rpm'] is None and identity == 'openrouter_free' and 'rpm' not in item:
            spec['rpm'] = config.mt_rpm()
        if not _integer(spec['rpm'], 0, 1000000):
            raise _invalid()
        spec['base_url'] = _base_url(provider, spec['base_url'], manifest)
        backend_identity = (provider, spec['base_url'], spec['model'], ref)
        if backend_identity in seen_backends:
            raise _invalid()
        seen_backends.add(backend_identity)
        result.append(spec)
    return result


def with_bailian_free_models(config, manifest, models):
    """Append known profiles without altering existing/custom pool entries."""
    if (not isinstance(models, list) or any(not isinstance(model, str) or model not in BAILIAN_FREE_MODELS
                                            for model in models) or len(set(models)) != len(models)):
        raise _invalid()
    specs = _configured_specs(config, manifest)
    raw = config.settings.mt_backends
    entries = ([{'id': identity} for identity in _defaults()] if raw is None else copy.deepcopy(raw))
    by_id = {item['id']: item for item in specs}
    for model in models:
        identity, response_format = BAILIAN_FREE_MODELS[model]
        capabilities = {'response_format': response_format, 'input_bytes': 32768, 'output_tokens': 4096}
        existing = next((item for item in specs if item['provider'] == 'bailian' and item['model'] == model), None)
        if existing is not None:
            if (existing['tier'] != 'free' or not existing['enabled'] or existing['capabilities'] != capabilities):
                raise ToolError('所选百炼模型与现有翻译池配置冲突；请先检查现有条目。', 'mt_config')
            continue
        if identity in by_id:
            raise ToolError('所选百炼模型的条目名称已被占用；请先检查现有翻译池。', 'mt_config')
        entry = {'id': identity, 'provider': 'bailian', 'base_url': None, 'model': model,
                 'credential_ref': 'bailian', 'tier': 'free', 'weight': 1, 'rpm': 60,
                 'capabilities': capabilities}
        entries.append(entry)
    return entries


def configured_route(config, manifest, *, paid_enabled=None):
    """Freeze a new route; missing optional keys are unavailable, never substituted."""
    if not isinstance(manifest, dict):
        raise _invalid()
    settings = config.settings
    routing = {'paid_enabled': True, 'free_wait_seconds': 15, 'stage_deadline_seconds': 180,
               'http_timeout_seconds': 60, 'max_attempts': 2}
    if settings.mt_routing is not None:
        if not isinstance(settings.mt_routing, dict) or set(settings.mt_routing) - {'paid_enabled', 'free_wait_seconds'}:
            raise _invalid()
        routing.update(settings.mt_routing)
    if paid_enabled is not None:
        routing['paid_enabled'] = paid_enabled
    if not _routing(routing):
        raise _invalid()

    quality = {'input_bytes': 32768, 'output_tokens': 4096, 'max_revisions': 2, 'manual_glossary': {}}
    if settings.mt_quality is not None:
        if not isinstance(settings.mt_quality, dict) or set(settings.mt_quality) - {'manual_glossary'}:
            raise _invalid()
        quality.update(copy.deepcopy(settings.mt_quality))
    if not _quality(quality):
        raise _invalid()

    try:
        specs = _configured_specs(config, manifest)
    except ToolError:
        raise _invalid() from None
    # The ASR account remains authoritative even if this pool has no Bailian model.
    account_key = config.get_key()
    if not isinstance(account_key, str) or not account_key:
        raise _invalid()
    if _fingerprint(account_key) != manifest.get('account'):
        raise ToolError('翻译池的百炼账户与任务不一致。', 'cache_mismatch')
    keys = {'bailian': account_key}
    for ref, get in [('qiniu', config.get_qiniu_key), ('openrouter', config.get_mt_key)]:
        if not any(spec['enabled'] and spec['credential_ref'] == ref for spec in specs):
            continue
        try:
            keys[ref] = get()
        except ToolError as error:
            if error.code != 'credentials':
                raise
            keys[ref] = None
        if keys[ref] is not None and (not isinstance(keys[ref], str) or not keys[ref]):
            raise _invalid()

    entries = []
    for spec in specs:
        key = keys.get(spec['credential_ref']) if spec['enabled'] else None
        backend = (MTBackend(spec['provider'], spec['base_url'], spec['model'], _fingerprint(key), 5).snapshot()
                   if key else None)
        entries.append({'id': spec['id'], 'tier': spec['tier'], 'weight': spec['weight'], 'rpm': spec['rpm'],
                        'credential_ref': spec['credential_ref'], 'backend': backend,
                        'capabilities': copy.deepcopy(spec['capabilities'])})
    if not any(entry['backend'] and (entry['tier'] == 'free' or routing['paid_enabled']) for entry in entries):
        raise ToolError('翻译池没有可用凭据；请配置免费服务或允许付费服务。', 'credentials')
    return {'format_version': 1, 'entries': entries, 'routing': routing, 'quality': quality}


def saved_route(manifest):
    """Validate only the saved contract and frozen identities, independent of defaults."""
    if not isinstance(manifest, dict):
        raise _invalid()
    snapshot = manifest.get('mt_route_snapshot')
    if (not isinstance(snapshot, dict) or set(snapshot) != {'format_version', 'entries', 'routing', 'quality'} or
            type(snapshot['format_version']) is not int or snapshot['format_version'] != 1 or
            type(manifest.get('translation_strategy_version')) is not int or
            manifest['translation_strategy_version'] != 5 or not isinstance(snapshot['entries'], list) or
            not snapshot['entries'] or not _routing(snapshot['routing']) or not _quality(snapshot['quality'])):
        raise _invalid()

    snapshot = copy.deepcopy(snapshot)
    identities, backends, fingerprints = set(), set(), {}
    for entry in snapshot['entries']:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise _invalid()
        identity, ref = entry['id'], entry['credential_ref']
        if (not _identifier(identity) or identity in identities or not isinstance(ref, str) or
                ref not in _PROVIDER_REFS.values() or not isinstance(entry['tier'], str) or
                entry['tier'] not in {'free', 'paid'} or not _integer(entry['weight'], 1, 100) or
                not _integer(entry['rpm'], 0, 1000000) or not _capabilities(entry['capabilities'])):
            raise _invalid()
        identities.add(identity)
        saved = entry['backend']
        if saved is None:
            continue
        if (not isinstance(saved, dict) or set(saved) != _BACKEND_FIELDS or
                type(saved['format_version']) is not int or saved['format_version'] != 1 or
                type(saved['request_version']) is not int or saved['request_version'] != 5 or
                not isinstance(saved['provider'], str) or saved['provider'] not in _PROVIDER_REFS or
                _PROVIDER_REFS[saved['provider']] != ref or not _model(saved['model']) or
                not isinstance(saved['base_url'], str) or not isinstance(saved['key_fingerprint'], str) or
                re.fullmatch(r'[0-9a-f]{64}', saved['key_fingerprint']) is None):
            raise _invalid()
        try:
            if _base_url(saved['provider'], saved['base_url'], manifest) != saved['base_url']:
                raise _invalid()
            backend = saved_mt({**manifest, 'mt_snapshot': saved, 'translation_strategy_version': 5})
        except ToolError:
            raise _invalid() from None
        backend_identity = (backend.provider, backend.base_url, backend.model, backend.key_fingerprint,
                            backend.request_version)
        if backend_identity in backends or ref in fingerprints and fingerprints[ref] != backend.key_fingerprint:
            raise _invalid()
        backends.add(backend_identity)
        fingerprints[ref] = backend.key_fingerprint

    primary = next((entry['backend'] for entry in snapshot['entries'] if entry['backend'] is not None), None)
    if (primary is None or manifest.get('mt_snapshot') != primary or not any(
            entry['backend'] is not None and (entry['tier'] == 'free' or snapshot['routing']['paid_enabled'])
            for entry in snapshot['entries'])):
        raise _invalid()
    return snapshot

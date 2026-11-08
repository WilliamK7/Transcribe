"""Freeze and validate gateway groups, permissions and cache identities."""
from __future__ import annotations

import copy
import re
import unicodedata

from .common import ToolError
from .mt_backend import MTBackend, key_fingerprint, normalize_mt_url


_FORMATS = {'json_object', 'json_schema', 'text'}
_ENTRY_FIELDS = {'id', 'tier', 'credential_ref', 'backend', 'capabilities'}
_BACKEND_FIELDS = {'format_version', 'provider', 'base_url', 'model', 'key_fingerprint', 'request_version'}
_CAPABILITY_FIELDS = {'response_format', 'input_bytes', 'output_tokens'}
_ROUTING_FIELDS = {'paid_enabled'}
_QUALITY_FIELDS = {'input_bytes', 'output_tokens', 'max_revisions', 'manual_glossary'}
_FORBIDDEN_UNICODE = {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}


def _invalid():
    return ToolError('翻译网关配置或任务快照无效；请检查设置或用 --reflow。', 'mt_config')


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


def _routing(value):
    return (isinstance(value, dict) and set(value) == _ROUTING_FIELDS and
            type(value['paid_enabled']) is bool)


def _quality(value):
    return (isinstance(value, dict) and set(value) == _QUALITY_FIELDS and
            _fixed_integer(value['input_bytes'], 32768) and _fixed_integer(value['output_tokens'], 4096) and
            _fixed_integer(value['max_revisions'], 2) and _manual_glossary(value['manual_glossary']))


def configured_route(config, manifest, *, paid_enabled=None):
    """Freeze the two gateway groups for new work; resume uses saved_route()."""
    if not isinstance(manifest, dict):
        raise _invalid()
    settings = config.mt_settings()
    if settings.mt_provider != 'magpie':
        raise ToolError('请先迁移或运行 --configure-mt magpie，并检查 MT_PROVIDER 环境覆盖。', 'mt_config')
    base = normalize_mt_url(settings.mt_base_url)
    models = [('magpie_free', 'free', settings.mt_free_model),
              ('magpie', 'paid', settings.mt_model or 'group/transcribe')]
    if any(not _model(model) for _, _, model in models) or models[0][2] == models[1][2]:
        raise _invalid()
    routing = {'paid_enabled': True}
    if settings.mt_routing is not None:
        if not isinstance(settings.mt_routing, dict) or set(settings.mt_routing) - {'paid_enabled'}:
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
    if _fingerprint(config.get_key()) != manifest.get('account'):
        raise ToolError('翻译的百炼账户与任务不一致。', 'cache_mismatch')
    fingerprint = _fingerprint(config.get_magpie_key(base))
    entries = [{'id': identity, 'tier': tier, 'credential_ref': 'magpie',
                'backend': MTBackend('magpie', base, model, fingerprint).snapshot(),
                'capabilities': {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}}
               for identity, tier, model in models]
    return {'format_version': 2, 'entries': entries, 'routing': routing, 'quality': quality}


def saved_route(manifest):
    """Validate only the saved contract and frozen identities, independent of defaults."""
    if not isinstance(manifest, dict):
        raise _invalid()
    snapshot = manifest.get('mt_route_snapshot')
    if (not isinstance(snapshot, dict) or set(snapshot) != {'format_version', 'entries', 'routing', 'quality'} or
            type(snapshot['format_version']) is not int or snapshot['format_version'] != 2 or
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
                ref != 'magpie' or not isinstance(entry['tier'], str) or
                entry['tier'] not in {'free', 'paid'} or not _capabilities(entry['capabilities'])):
            raise _invalid()
        identities.add(identity)
        saved = entry['backend']
        if saved is None:
            continue
        if (not isinstance(saved, dict) or set(saved) != _BACKEND_FIELDS or
                type(saved['format_version']) is not int or saved['format_version'] != 1 or
                type(saved['request_version']) is not int or saved['request_version'] != 5 or
                saved['provider'] != 'magpie' or not _model(saved['model']) or
                not isinstance(saved['base_url'], str) or not isinstance(saved['key_fingerprint'], str) or
                re.fullmatch(r'[0-9a-f]{64}', saved['key_fingerprint']) is None):
            raise _invalid()
        try:
            if normalize_mt_url(saved['base_url']) != saved['base_url']:
                raise _invalid()
        except ToolError:
            raise _invalid() from None
        backend = MTBackend.from_snapshot(saved)
        backend_identity = (backend.provider, backend.base_url, backend.model, backend.key_fingerprint,
                            backend.request_version)
        if backend_identity in backends or ref in fingerprints and fingerprints[ref] != backend.key_fingerprint:
            raise _invalid()
        backends.add(backend_identity)
        fingerprints[ref] = backend.key_fingerprint

    if not any(
            entry['backend'] is not None and (entry['tier'] == 'free' or snapshot['routing']['paid_enabled'])
            for entry in snapshot['entries']):
        raise _invalid()
    return snapshot

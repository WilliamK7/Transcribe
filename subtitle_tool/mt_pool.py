"""Freeze and validate gateway groups, permissions and cache identities."""
from __future__ import annotations

import copy
import re
import unicodedata

from .common import ToolError
from .mt_backend import MTBackend, key_fingerprint, normalize_mt_url


_ENTRY_FIELDS = {'id', 'tier', 'credential_ref', 'backend', 'capabilities'}
_BACKEND_FIELDS = {'format_version', 'provider', 'base_url', 'model', 'key_fingerprint', 'request_version', 'policy_version'}
_CAPABILITY_FIELDS = {'response_format', 'input_bytes', 'output_tokens'}
_ROUTING_STRATEGY = 'free_first'
_QUALITY_FIELDS = {'input_bytes', 'output_tokens', 'max_revisions', 'manual_glossary'}
_FORBIDDEN_UNICODE = {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}
_ENTRY_TIERS = {name: tier for role in ('', '_review', '_judge') for name, tier in
                [('magpie' + role, 'mixed'), ('magpie' + role + '_fallback', 'paid')]}


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


def _manual_glossary(value):
    return (isinstance(value, dict) and len(value) <= 200 and
            all(_text(source, 200) and _text(target, 500) for source, target in value.items()))


def _capabilities(value):
    return (isinstance(value, dict) and set(value) == _CAPABILITY_FIELDS and
            value['response_format'] == 'json_object' and
            _integer(value['input_bytes'], 1, 1048576) and _integer(value['output_tokens'], 1, 32768))


def _quality(value):
    return (isinstance(value, dict) and set(value) == _QUALITY_FIELDS and
            _fixed_integer(value['input_bytes'], 32768) and _fixed_integer(value['output_tokens'], 4096) and
            _fixed_integer(value['max_revisions'], 2) and _manual_glossary(value['manual_glossary']))


def configured_route(config, manifest):
    """Freeze primary groups and the paid validation fallback for new work."""
    if not isinstance(manifest, dict):
        raise _invalid()
    settings = config.mt_settings()
    if not _integer(settings.mt_policy_version, 1, 2**31 - 1):
        raise ToolError('mt_policy_version 必须是 1～2147483647 的整数；更换翻译策略时递增。', 'mt_config')
    base = normalize_mt_url(settings.mt_base_url)
    models = [('magpie', 'mixed', settings.mt_model or 'group/transcribe'),
              ('magpie_fallback', 'paid', settings.mt_fallback_model),
              ('magpie_review', 'mixed', settings.mt_review_model),
              ('magpie_review_fallback', 'paid', settings.mt_review_fallback_model),
              ('magpie_judge', 'mixed', settings.mt_judge_model),
              ('magpie_judge_fallback', 'paid', settings.mt_judge_fallback_model)]
    if any(not _model(model) for _, _, model in models) or len({model for _, _, model in models}) != 6:
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
                'backend': MTBackend('magpie', base, model, fingerprint,
                                     policy_version=settings.mt_policy_version).snapshot(),
                'capabilities': {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}}
               for identity, tier, model in models]
    from .magpie import bound_gateway
    from .mt_catalog import saved_catalog
    identities = {}
    if bound_gateway(settings, base):
        from .magpie import group_models
        from .mt_catalog import read_registry, offer_billing
        from .mt_identity import canonical_model, dynamic_model
        catalog = saved_catalog(config)
        identities = copy.deepcopy(catalog.get('identities', {}))
        registry = read_registry(settings, catalog)
        for _, _, model in models:
            for owner, name, provider in group_models(registry, model):
                member = owner + '/' + name
                if member not in identities and not dynamic_model(member):
                    identities[member] = {'models': [canonical_model(member)], 'dynamic': False,
                                          'billing': offer_billing(provider, name)}
    from .mt_identity import valid_identity_map
    if not valid_identity_map(identities):
        raise _invalid()
    return {'format_version': 6, 'entries': entries, 'routing': {'strategy': _ROUTING_STRATEGY},
            'quality': quality, 'identities': identities}


def saved_route(manifest):
    """Validate saved gateway groups and frozen task identities."""
    if not isinstance(manifest, dict):
        raise _invalid()
    snapshot = manifest.get('mt_route_snapshot')
    from .mt_identity import valid_identity_map
    fields = {'format_version', 'entries', 'routing', 'quality', 'identities'}
    if (not isinstance(snapshot, dict) or set(snapshot) != fields or
            type(snapshot['format_version']) is not int or snapshot['format_version'] != 6 or
            not valid_identity_map(snapshot['identities']) or
            type(manifest.get('translation_strategy_version')) is not int or
            manifest['translation_strategy_version'] != 6 or not isinstance(snapshot['entries'], list) or
            len(snapshot['entries']) != 6 or snapshot['routing'] != {'strategy': _ROUTING_STRATEGY} or
            not _quality(snapshot['quality'])):
        raise _invalid()

    snapshot = copy.deepcopy(snapshot)
    identities, backends, fingerprints, versions = set(), set(), set(), set()
    for entry in snapshot['entries']:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise _invalid()
        identity = entry['id']
        if (not isinstance(identity, str) or identity not in _ENTRY_TIERS or identity in identities or
                entry['credential_ref'] != 'magpie' or
                entry['tier'] != _ENTRY_TIERS[identity] or not _capabilities(entry['capabilities'])):
            raise _invalid()
        identities.add(identity)
        saved = entry['backend']
        if (not isinstance(saved, dict) or set(saved) != _BACKEND_FIELDS or
                type(saved['format_version']) is not int or saved['format_version'] != 2 or
                type(saved['request_version']) is not int or saved['request_version'] != 6 or
                not _integer(saved['policy_version'], 1, 2**31 - 1) or
                saved['provider'] != 'magpie' or not _model(saved['model']) or
                not isinstance(saved['base_url'], str) or not isinstance(saved['key_fingerprint'], str) or
                re.fullmatch(r'[0-9a-f]{64}', saved['key_fingerprint']) is None):
            raise _invalid()
        try:
            if normalize_mt_url(saved['base_url']) != saved['base_url']:
                raise _invalid()
        except ToolError:
            raise _invalid() from None
        backend_identity = (saved['base_url'], saved['model'])
        if backend_identity in backends:
            raise _invalid()
        backends.add(backend_identity)
        fingerprints.add(saved['key_fingerprint'])
        versions.add(saved['policy_version'])

    if len(fingerprints) != 1 or len(versions) != 1:
        raise _invalid()
    return snapshot

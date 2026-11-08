"""Import existing credentials through magpie's CLI, without logging secrets."""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import requests

from .common import ToolError, atomic_bytes, atomic_json, validate_workspace_id
from .config import Config
from .mt_backend import OPENROUTER_BASE_URL, QINIU_BASE_URL, QINIU_FREE_MODEL, STRUCTURED_BAILIAN_MODEL, bailian_mt_url, normalize_mt_url, qiniu_free_active

from .mt_pool import _invalid, _identifier, _integer, _model, _capabilities


DEFAULT_ENTRIES = (
    ('qiniu_free', 'free', 3, 'qiniu', 'qiniu', QINIU_BASE_URL, QINIU_FREE_MODEL),
    ('openrouter_free', 'free', 1, 'openrouter', 'openai_compatible', OPENROUTER_BASE_URL, 'openrouter/free'),
    ('qiniu_paid', 'paid', 3, 'qiniu', 'qiniu', QINIU_BASE_URL, 'deepseek/deepseek-v4.1-flash'),
    ('bailian_paid', 'paid', 1, 'bailian', 'bailian', None, STRUCTURED_BAILIAN_MODEL),
)

_PROVIDER_REFS = {'qiniu': 'qiniu', 'openai_compatible': 'openrouter', 'bailian': 'bailian'}
_CONFIG_FIELDS = {'id', 'provider', 'base_url', 'model', 'credential_ref', 'tier', 'weight', 'rpm', 'enabled', 'capabilities'}


def _default_capabilities(provider):
    return {'response_format': 'json_object' if provider == 'qiniu' else 'json_schema',
            'input_bytes': 32768, 'output_tokens': 4096}


def _base_url(provider, value, manifest):
    if provider in {'openai_compatible', 'magpie'}:
        return normalize_mt_url(value)
    expected = QINIU_BASE_URL if provider == 'qiniu' else bailian_mt_url(manifest)
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


def _source_specs(config, manifest):
    selected = config.mt_settings()
    if selected.mt_provider == 'magpie':
        raise ToolError('请从迁移前的直连设置导入。', 'magpie_migration')
    raw = config.settings.mt_backends
    templates = _defaults()
    # An explicit list defines the complete pool, in order. Existing template
    # IDs may be written as short overrides; omitted IDs are absent.
    if raw is None:
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


def _run(executable, *args):
    try:
        result = subprocess.run([str(executable), *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise ValueError()
    except (OSError, ValueError, subprocess.SubprocessError):
        # CalledProcessError and the CLI output may contain a provider's key.
        raise ToolError('magpie 配置命令失败；原项目设置尚未切换，可检查网关后重新运行。', 'magpie_migration') from None


def migrate(config, executable, data_dir, *, apply=False):
    executable, data_dir = Path(executable).resolve(), Path(data_dir).resolve()
    registry = data_dir / 'providers.json'
    try:
        before = registry.read_bytes()
        current = json.loads(before)
        providers = {row['id']: row for row in current['providers']}
        groups = {row['id']: row for row in current.get('groups', [])}
        if not executable.is_file():
            raise ValueError()
        if not (executable.parent / '.portable').is_file() or data_dir != executable.parent / 'data':
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise ToolError('无法读取指定 magpie 安装的入口配置。', 'magpie_migration') from None
    if config.settings.asr_provider == 'magpie' and config.settings.mt_provider == 'magpie':
        config.get_key()
        return {'status': 'already_configured', 'provider': 'magpie'}
    if any(os.environ.get(name) for name in ('MT_PROVIDER', 'MT_BASE_URL', 'MT_MODEL', 'MT_FREE_MODEL')):
        raise ToolError('请先清除 MT_PROVIDER／MT_BASE_URL／MT_MODEL／MT_FREE_MODEL 覆盖，再迁移保存设置。', 'magpie_migration')
    workspace = validate_workspace_id(config.settings.workspace_id)
    asr_key = config.get_key()
    base = config.settings.magpie_base_url
    from .magpie import local_gateway
    from .mt_backend import normalize_mt_url
    base = normalize_mt_url(base)
    if not local_gateway(base):
        raise ToolError('自动导入只支持本机常驻 magpie。', 'magpie_migration')
    try:
        response = requests.get(base.rsplit('/v1', 1)[0] + '/api/hello', timeout=10, allow_redirects=False)
        hello = response.json()
        response.close()
        version = tuple(int(part) for part in hello['version'].split('.'))
        if hello['name'] != 'magpie' or version < (0, 1, 3323):
            raise ValueError()
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise ToolError('需要运行支持原生 API 转发的 magpie（已验证版本 0.1.3323）。', 'magpie_migration') from None

    planned = []
    free, paid = [], []
    skipped = []
    def add(identity, url, key, models, *, cost='paid', off=False, rpm=0):
        planned.append(dict(id=identity, url=url, key=key, models=models, free=cost == 'free', off=off, rpm=rpm))
        if not off and models and cost in {'free', 'paid'}:
            (free if cost == 'free' else paid).extend(identity + '/' + model for model in models)

    asr_url = f'https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
    from .mt_backend import key_fingerprint
    from .magpie import known_free_model
    specs = _source_specs(config, {'region': 'beijing', 'workspace_id': workspace,
        'access_mode': 'workspace', 'account': key_fingerprint(asr_key)})
    buckets = {}
    for spec in specs:
        if spec['provider'] == 'magpie':
            raise ToolError('翻译已选择 magpie；自动导入须从迁移前的直连设置运行。', 'magpie_migration')
        ref, url, model, tier = spec['credential_ref'], spec['base_url'], spec['model'], spec['tier']
        bucket = (ref, url, tier, spec['enabled'])
        buckets.setdefault(bucket, []).append(spec)
    for (ref, url, tier, enabled), members in buckets.items():
        try:
            key = {'bailian': lambda: asr_key, 'qiniu': config.get_qiniu_key,
                   'openrouter': config.get_mt_key}[ref]()
        except ToolError:
            skipped.append(ref + '_missing_key')
            continue
        models = [member['model'] for member in members]
        standard = ((ref == 'qiniu' and url == QINIU_BASE_URL) or
                    (ref == 'openrouter' and url == OPENROUTER_BASE_URL) or ref == 'bailian')
        identity = ('transcribe-' + ref + '-' + tier) if standard else ('transcribe-' + members[0]['id'])
        if ref == 'bailian':
            identity = 'transcribe-bailian' if tier == 'paid' and enabled else 'transcribe-bailian-trial'
        if not enabled and ref != 'bailian':
            identity += '-disabled'
        protected = tier != 'free' or all(known_free_model(url, model) for model in models)
        expired = url == QINIU_BASE_URL and QINIU_FREE_MODEL in models and not qiniu_free_active()
        rates = [member['rpm'] for member in members if member['rpm']]
        rpm = min(rates) if rates else (20 if url == OPENROUTER_BASE_URL and tier == 'free' else 0)
        add(identity, url, key, models, cost=tier, off=not enabled or not protected or expired, rpm=rpm)
        if not protected:
            skipped.append(identity + '_requires_verified_free_protection')
        if expired:
            skipped.append('qiniu_promotion_expired')
    if not any(item['id'] == 'transcribe-bailian' for item in planned):
        add('transcribe-bailian', asr_url, asr_key, [], cost='native')
    add('transcribe-upload', 'https://dashscope.aliyuncs.com/compatible-mode/v1', asr_key, [], cost='native')
    if not free:
        raise ToolError('现有配置中没有可导入的免费服务凭据；未改变项目配置。', 'magpie_migration')
    if len({item['id'] for item in planned}) != len(planned):
        raise ToolError('迁移入口名称冲突，请先为原配置使用不同的入口 ID。', 'magpie_migration')
    target_groups = {'transcribe-free': free, 'transcribe': ['group/transcribe-free', *paid]}
    receipt_path = config.data / 'magpie-migration.json'
    previous_receipt = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
    owned = set(previous_receipt.get('provider_ids', []))
    owned_groups = set(previous_receipt.get('group_ids', []))
    if any(item['id'] in providers and item['id'] not in owned for item in planned) or any(
            identity in groups and identity not in owned_groups for identity in target_groups):
        raise ToolError('magpie 已有同名字幕入口或组；未覆盖，请先核对归属。', 'magpie_migration')
    result = {'status': 'planned', 'provider': 'magpie', 'version': hello['version'],
              'provider_ids': [item['id'] for item in planned], 'group_ids': list(target_groups),
              'free_members': free, 'paid_members': paid, 'disabled_reasons': skipped}
    if not apply:
        return result
    backup = config.data / 'migrations' / ('magpie-' + uuid.uuid4().hex)
    atomic_bytes(backup / 'providers.json', before)
    settings_path = config.data / 'settings.json'
    if settings_path.exists():
        atomic_bytes(backup / 'settings.json', settings_path.read_bytes())
    atomic_json(receipt_path, {**result, 'status': 'importing', 'backup': str(backup)})
    for item in planned:
        identity = item['id']
        args = ['provider', 'set' if identity in providers else 'add', identity]
        args += ['url=' + item['url'], 'key=' + item['key'], 'list=off',
                 'models=' + ','.join(item['models']), 'free=' + ('yes' if item['free'] else 'no')]
        if identity not in {'transcribe-bailian', 'transcribe-upload'}:
            # magpie deduplicates equal key + URLs + headers. A harmless route
            # header keeps free and paid catalogs separate on the same key.
            args.append('header.X-Transcribe-Route=' + identity)
        if identity not in providers:
            args.append('id=' + identity)
        _run(executable, *args)
        _run(executable, 'provider', 'fallback', identity, 'none')
        _run(executable, 'provider', 'rpm', identity, str(item['rpm']))
        if not item['models']:
            _run(executable, 'provider', 'models', identity, 'none')
        if item['off']:
            _run(executable, 'provider', 'off', identity)
    for identity, members in target_groups.items():
        _run(executable, 'group', 'set' if identity in groups else 'add', identity,
             'models=' + ','.join(members), 'routing=' + ('free' if identity == 'transcribe' else 'order'), 'stays=off')
    imported = json.loads(registry.read_bytes())
    imported_providers = {row['id']: row for row in imported['providers']}
    imported_groups = {row['id']: row for row in imported.get('groups', [])}
    if any(imported_providers.get(item['id'], {}).get('chat') != item['url'] or
           imported_providers.get(item['id'], {}).get('key') != item['key'] for item in planned) or any(
            imported_groups.get(identity, {}).get('members') != members for identity, members in target_groups.items()):
        raise ToolError('magpie 导入结果与计划不符；项目设置尚未切换。', 'magpie_migration')
    selected = replace(config.settings, asr_provider='magpie', magpie_data_dir=str(data_dir),
                       magpie_asr_provider='transcribe-bailian', magpie_upload_provider='transcribe-upload',
                       mt_provider='magpie', mt_backends=None, mt_base_url=base,
                       mt_routing={'paid_enabled': (config.settings.mt_routing or {}).get('paid_enabled', True)},
                       mt_model='group/transcribe', mt_free_model='group/transcribe-free')
    from .magpie import asr_providers
    asr_providers(selected, workspace)
    old_settings = config.settings
    config.settings = selected
    try:
        config.save()
    except Exception:
        config.settings = old_settings
        raise
    result.update(status='configured', backup=str(backup))
    atomic_json(receipt_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='将现有字幕凭据导入 magpie，默认仅检查迁移计划。')
    parser.add_argument('--magpie', required=True, type=Path, help='magpie.com 的绝对路径')
    parser.add_argument('--data-dir', required=True, type=Path, help='常驻 magpie 使用的 data 目录')
    parser.add_argument('--apply', action='store_true', help='备份后导入入口、创建字幕组并切换项目设置')
    args = parser.parse_args(argv)
    try:
        result = migrate(Config(), args.magpie, args.data_dir, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ToolError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('迁移未完成；已有备份及用户数据保留，请检查配置。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

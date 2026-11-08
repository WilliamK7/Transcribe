"""Identity and fee boundaries for the locally managed magpie providers."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .common import ToolError, validate_workspace_id
from .mt_backend import normalize_mt_url


def local_gateway(base_url):
    host = urlsplit(base_url).hostname
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def bound_gateway(settings, base_url):
    """A registry's fee/account proof applies only to its saved local listener."""
    if not settings.magpie_data_dir:
        return False
    try:
        path = Path(settings.magpie_data_dir) / 'settings.json'
        saved = json.loads(path.read_bytes()) if path.exists() else {}
        port = saved.get('port') or 3425
        endpoint = urlsplit(normalize_mt_url(base_url))
        return (type(port) is int and 1 <= port <= 65535 and local_gateway(base_url) and
                endpoint.path == '/v1' and endpoint.port == port)
    except (OSError, ValueError, TypeError, ToolError):
        return False


def asr_providers(settings, workspace):
    """Retain account/workspace checks even though magpie signs the requests."""
    error = ToolError('magpie 的百炼／上传入口与当前账户或工作空间不一致；请检查网关配置。', 'magpie_identity')
    try:
        if not bound_gateway(settings, settings.magpie_base_url):
            raise error
        ids = (settings.magpie_asr_provider, settings.magpie_upload_provider)
        if (not settings.magpie_data_dir or any(not isinstance(x, str) or
                re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', x) is None for x in ids) or ids[0] == ids[1]):
            raise error
        values = json.loads((Path(settings.magpie_data_dir) / 'providers.json').read_text(encoding='utf-8-sig'))
        rows = values['providers']
        if not isinstance(rows, list):
            raise error
        selected = []
        hosts = [f'https://{validate_workspace_id(workspace)}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                 'https://dashscope.aliyuncs.com/compatible-mode/v1']
        for identity, expected in zip(ids, hosts):
            matches = [row for row in rows if isinstance(row, dict) and row.get('id') == identity]
            if len(matches) != 1:
                raise error
            row = matches[0]
            if (row.get('off') or not isinstance(row.get('key'), str) or not row['key'].strip() or
                    normalize_mt_url(row.get('chat')) != expected or row.get('keys') or row.get('headers')):
                raise error
            selected.append(row)
        if selected[0]['key'] != selected[1]['key']:
            raise error
        return tuple(selected)
    except (OSError, ValueError, KeyError, TypeError, ToolError):
        raise error from None


def known_free_model(base_url, model):
    from .mt_backend import OPENROUTER_BASE_URL, QINIU_BASE_URL, QINIU_FREE_MODEL
    return ((base_url == QINIU_BASE_URL and model == QINIU_FREE_MODEL) or
            (base_url == OPENROUTER_BASE_URL and (model == 'openrouter/free' or model.endswith(':free'))))


def _pause_expired_promotion(settings, identity, provider):
    """Disable only the designated promotional endpoint through magpie."""
    from .mt_backend import QINIU_BASE_URL, QINIU_FREE_MODEL
    folder = Path(settings.magpie_data_dir)
    executable = folder.parent / 'magpie.com'
    if (identity != 'transcribe-qiniu-free' or provider.get('chat') != QINIU_BASE_URL or
            provider.get('models') != [QINIU_FREE_MODEL] or folder.name != 'data' or
            not (folder.parent / '.portable').is_file() or not executable.is_file()):
        return False
    try:
        result = subprocess.run([str(executable), 'provider', 'off', identity],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            return False
        current = json.loads((folder / 'providers.json').read_bytes())
        return any(row.get('id') == identity and row.get('off') is True for row in current['providers'])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def validate_group(settings, model, *, free_only, base_url=None):
    """Check the local fee boundary; routing and credentials stay in magpie.

    Remote gateways are administered by their owner. A local registry can
    detect accidental paid fallback or an expired promotion.
    """
    from .mt_backend import QINIU_FREE_MODEL, qiniu_free_active
    error = ToolError('magpie 字幕组不可用或包含未获免费保证的回退；请在 magpie 检查组成员和活动有效期。', 'magpie_group')
    if not bound_gateway(settings, base_url or settings.magpie_base_url):
        if free_only:
            raise error
        return
    try:
        raw = json.loads((Path(settings.magpie_data_dir) / 'providers.json').read_text(encoding='utf-8-sig'))
        providers = {row['id']: row for row in raw['providers']}
        groups = {row['id']: row for row in raw.get('groups', [])}
        def visit(member, seen):
            if not isinstance(member, str) or member in seen or len(seen) > 8:
                raise error
            owner, name = member.split('/', 1)
            if owner == 'group':
                group = groups[name]
                if (group.get('hidden') or not group.get('members') or group.get('match') or
                        (free_only and (group.get('classifier') or group.get('rules')))):
                    raise error
                for nested in group['members']:
                    if nested not in group.get('off', []):
                        visit(nested, seen | {member})
            else:
                provider = providers[owner]
                if provider.get('off'):
                    return
                plain_model = re.sub(r':(?:none|minimal|low|medium|high|xhigh|max)$', '', name)
                if free_only and (provider.get('fallback') or not known_free_model(
                        normalize_mt_url(provider.get('chat')), plain_model)):
                    raise error
                if plain_model == QINIU_FREE_MODEL and not qiniu_free_active():
                    if not _pause_expired_promotion(settings, owner, provider):
                        raise error
        visit(model, set())
    except (OSError, ValueError, KeyError, TypeError, ToolError):
        raise error from None

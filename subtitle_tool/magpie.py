"""Identity and fee boundaries for the locally managed magpie providers."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .common import ToolError
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


def validate_group(settings, model, *, free_only, base_url=None, paid_only=False, verified_free=()):
    """Check the local fee boundary; routing and credentials stay in magpie.

    Remote gateways are administered by their owner. A local registry can
    detect accidental paid fallback or an expired promotion.
    """
    from .mt_backend import QINIU_FREE_MODEL, qiniu_free_active
    error = ToolError(('magpie 付费后备组不可用或仍包含免费成员；请检查独立的付费组。' if paid_only else
                      'magpie 字幕组不可用或包含未获免费保证的回退；请在 magpie 检查组成员和活动有效期。'), 'magpie_group')
    if not bound_gateway(settings, base_url or settings.mt_base_url):
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
                        ((free_only or paid_only) and (group.get('classifier') or group.get('rules')))):
                    raise error
                for nested in group['members']:
                    if nested not in group.get('off', []):
                        visit(nested, seen | {member})
            else:
                provider = providers[owner]
                if provider.get('off'):
                    return
                plain_model = re.sub(r':(?:none|minimal|low|medium|high|xhigh|max)$', '', name)
                trial = owner == 'transcribe-bailian-trial'
                proven = (owner, plain_model) in verified_free
                if trial and not proven:
                    raise error
                if free_only and (provider.get('fallback') or not known_free_model(
                        normalize_mt_url(provider.get('chat')), plain_model) and not proven):
                    raise error
                if paid_only and (trial or provider.get('fallback') or known_free_model(
                        normalize_mt_url(provider.get('chat')), plain_model)):
                    raise error
                if plain_model == QINIU_FREE_MODEL and not qiniu_free_active():
                    if not _pause_expired_promotion(settings, owner, provider):
                        raise error
        visit(model, set())
    except (OSError, ValueError, KeyError, TypeError, ToolError):
        raise error from None

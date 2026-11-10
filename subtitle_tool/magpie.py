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


def billing_route(settings, model):
    """Classify the selected offer; this is not a receipt or cash estimate."""
    try:
        owner, name = model.split('/', 1)
        raw = json.loads((Path(settings.magpie_data_dir) / 'providers.json').read_bytes())
        provider = next(p for p in raw['providers'] if p['id'] == owner)
        if owner.startswith('transcribe-bailian-trial-'):
            return 'trial'
        if provider.get('countFree') or known_free_model(normalize_mt_url(provider.get('chat')), name):
            return 'free'
        return 'commercial'
    except (OSError, ValueError, KeyError, TypeError, StopIteration, ToolError):
        return 'unknown'


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


def group_models(raw, model):
    providers = {row['id']: row for row in raw['providers']}
    groups = {row['id']: row for row in raw.get('groups', [])}
    def visit(member, seen):
        if not isinstance(member, str) or member in seen or len(seen) > 8:
            raise ValueError()
        owner, name = member.split('/', 1)
        if owner == 'group':
            group = groups[name]
            if group.get('hidden'):
                return []
            if group.get('match') or group.get('classifier') or group.get('rules'):
                raise ValueError()
            return [leaf for nested in group.get('members', []) if nested not in group.get('off', [])
                    for leaf in visit(nested, seen | {member})]
        provider = providers[owner]
        if provider.get('off'):
            return []
        if provider.get('fallback'):
            raise ValueError()
        plain = re.sub(r':(?:none|minimal|low|medium|high|xhigh|max)$', '', name)
        return [(owner, plain, provider)]
    return visit(model, set())


def validate_group(settings, model, *, base_url=None, paid_only=False):
    """Validate enabled local routes, preserving account identity in the gateway."""
    if not bound_gateway(settings, base_url or settings.mt_base_url):
        return
    from .mt_backend import QINIU_FREE_MODEL, qiniu_free_active
    try:
        raw = json.loads((Path(settings.magpie_data_dir) / 'providers.json').read_bytes())
        leaves = group_models(raw, model)
        if not leaves:
            raise ValueError()
        for owner, name, provider in leaves:
            trial = owner.startswith('transcribe-bailian-trial')
            if paid_only and (trial or known_free_model(normalize_mt_url(provider.get('chat')), name)):
                raise ValueError()
            if name == QINIU_FREE_MODEL and not qiniu_free_active():
                if not _pause_expired_promotion(settings, owner, provider):
                    raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, ToolError):
        raise ToolError('magpie 字幕角色组不可用，请检查组成员、账户和商业后备。', 'magpie_group') from None


def validate_role_pools(settings, entries, *, base_url=None):
    from .mt_admission import canonical_model
    if not bound_gateway(settings, base_url or settings.mt_base_url):
        return
    try:
        raw = json.loads((Path(settings.magpie_data_dir) / 'providers.json').read_bytes())
        identities = {}
        for role, models in entries.items():
            for _, name, _ in [leaf for model in ([models] if isinstance(models, str) else models)
                               for leaf in group_models(raw, model)]:
                identity = canonical_model(name)
                if identity in identities and identities[identity] != role:
                    raise ValueError()
                identities[identity] = role
    except (OSError, ValueError, KeyError, TypeError):
        raise ToolError('正文、复核和裁决组需要使用互不重叠的底层型号。', 'magpie_group') from None

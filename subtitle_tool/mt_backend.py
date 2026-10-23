"""MT connection identity, independent of the saved ASR account and contract."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import asdict, dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from .common import API_ROOTS, MT_MODEL, ToolError, connection_identity


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def key_fingerprint(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_mt_url(value):
    error = ToolError("mt_base_url 必须是无凭据、查询参数或片段的 HTTPS 根地址；回环地址可用 HTTP，不能填写 completions 路径。", "mt_config")
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise error
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        decoded_path = unquote(parsed.path).lower().rstrip("/")
        if (not host or parsed.username is not None or parsed.password is not None or
                "?" in value or "#" in value or "\\" in value or
                "%" in parsed.netloc or decoded_path.endswith("/completions") or
                any(part in {".", ".."} for part in decoded_path.split("/"))):
            raise error
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise error
        # Preserve the provider's path prefix; normalize host, default port and trailing slashes.
        netloc = f"[{host.lower()}]" if ":" in host else host.lower().encode("idna").decode("ascii")
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            netloc += f":{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise error from None


def bailian_mt_url(manifest):
    identity = connection_identity(manifest)
    base = (f"https://{identity['workspace_id']}.cn-beijing.maas.aliyuncs.com"
            if identity["access_mode"] == "workspace" else API_ROOTS["beijing"])
    return base + "/compatible-mode/v1"


@dataclass(frozen=True)
class MTBackend:
    provider: str
    base_url: str
    model: str
    key_fingerprint: str
    request_version: int = 1

    @property
    def diagnostic_provider(self):
        if self.provider == 'openai_compatible' and self.base_url == OPENROUTER_BASE_URL:
            return 'openrouter'
        return self.provider

    def snapshot(self):
        return {"format_version": 1, **asdict(self)}

    def request_options(self):
        return {"provider": self.provider, "model": self.model, "request_version": self.request_version}

    def cache_scope(self):
        # Bailian's fixed model, endpoint and Key are already in the historical ASR scope.
        # Keeping that algorithm also lets an explicit reflow reuse exact old requests.
        return {"mt_identity": self.snapshot()} if self.provider == "openai_compatible" else {}

    def credential(self, config):
        key = config.get_mt_key() if self.provider == "openai_compatible" else config.get_key()
        if key_fingerprint(key) != self.key_fingerprint:
            raise ToolError("此任务的翻译 Key 已变更；请恢复原 MT Key 后 --resume，或用 --reflow 显式切换翻译配置。", "mt_account_changed")
        return key


def configured_mt(config, manifest, *, asr_key=None):
    settings = config.mt_settings()
    provider = settings.mt_provider
    if provider == "bailian":
        return MTBackend(provider, bailian_mt_url(manifest), MT_MODEL,
                         key_fingerprint(asr_key if asr_key is not None else config.get_key()))
    if provider != "openai_compatible":
        raise ToolError("mt_provider 必须为 bailian 或 openai_compatible。", "mt_config")
    base = normalize_mt_url(settings.mt_base_url)
    model = settings.mt_model
    if not isinstance(model, str) or not model.strip() or model != model.strip() or any(ord(c) < 32 for c in model):
        raise ToolError("启用兼容翻译后端必须填写有效的 mt_model。", "mt_config")
    config.mt_rpm()
    return MTBackend(provider, base, model, key_fingerprint(config.get_mt_key()),
                     request_version=2 if base == OPENROUTER_BASE_URL else 1)


def saved_mt(manifest):
    """Absent snapshots always mean historical Bailian, never current configuration."""
    if "mt_snapshot" not in manifest:
        return MTBackend("bailian", bailian_mt_url(manifest), MT_MODEL, manifest.get("account", ""))
    saved = manifest["mt_snapshot"]
    required = {"format_version", "provider", "base_url", "model", "key_fingerprint", "request_version"}
    if (not isinstance(saved, dict) or set(saved) != required or
            type(saved["format_version"]) is not int or saved["format_version"] != 1 or
            type(saved["request_version"]) is not int or saved["request_version"] not in {1, 2}):
        raise ToolError("任务的翻译请求契约不受支持，请用 --reflow 显式重排。", "cache_version")
    backend = MTBackend(**{k: v for k, v in saved.items() if k != "format_version"})
    if backend.request_version == 2 and (backend.provider != "openai_compatible" or
                                         backend.base_url != OPENROUTER_BASE_URL):
        raise ToolError("翻译请求契约 2 仅支持 OpenRouter 兼容端点，请用 --reflow 显式重排。", "cache_version")
    if (not isinstance(backend.key_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", backend.key_fingerprint) or
            not isinstance(backend.model, str) or not backend.model.strip() or
            backend.model != backend.model.strip() or any(ord(c) < 32 for c in backend.model)):
        raise ToolError("任务的翻译身份快照无效。", "cache_mismatch")
    if backend.provider == "bailian":
        if (backend.base_url != bailian_mt_url(manifest) or backend.model != MT_MODEL or
                backend.key_fingerprint != manifest.get("account")):
            raise ToolError("任务的百炼翻译身份与原账户范围不一致。", "cache_mismatch")
    elif backend.provider == "openai_compatible":
        if normalize_mt_url(backend.base_url) != backend.base_url:
            raise ToolError("任务的翻译地址快照无效。", "cache_mismatch")
        if manifest.get("translation_strategy_version") != 3:
            raise ToolError("兼容翻译后端需要批量翻译策略 3，请用 --reflow。", "cache_version")
    else:
        raise ToolError("任务的翻译后端不受支持，请用 --reflow 显式重排。", "cache_version")
    return backend

"""MT connection identity, independent of the saved ASR account and contract."""
from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit, urlunsplit

from .common import ToolError, connection_identity


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
QINIU_BASE_URL = "https://api.qnaigc.com/v1"
QINIU_FREE_MODEL = "qiniu/conatus-free"
STRUCTURED_BAILIAN_MODEL = "qwen3.8-flash"


def qiniu_free_active(now=None):
    """The advertised interval is inclusive through the last Beijing minute."""
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('A timezone-aware instant is required')
    beijing = timezone(timedelta(hours=8))
    return (datetime(2026, 10, 24, tzinfo=beijing) <= now.astimezone(beijing) <
            datetime(2026, 12, 1, tzinfo=beijing))


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
    base = f"https://{identity['workspace_id']}.cn-beijing.maas.aliyuncs.com"
    return base + "/compatible-mode/v1"


@dataclass(frozen=True)
class MTBackend:
    provider: str
    base_url: str
    model: str
    key_fingerprint: str
    request_version: int = 5


    def snapshot(self):
        return {"format_version": 1, **asdict(self)}

    @classmethod
    def from_snapshot(cls, saved):
        """Construct an identity from an entry validated by saved_route()."""
        return cls(**{key: value for key, value in saved.items() if key != 'format_version'})


    def credential(self, config):
        key = config.get_magpie_key(self.base_url)
        if key_fingerprint(key) != self.key_fingerprint:
            raise ToolError("此任务的翻译 Key 已变更；请恢复原 MT Key 后 --resume，或用 --reflow 显式切换翻译配置。", "mt_account_changed")
        return key

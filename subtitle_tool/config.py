from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .common import ToolError, atomic_bytes, atomic_json, validate_workspace_id


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    region: str = "beijing"
    workspace_id: str = ""
    mt_workers: int = 4
    mt_provider: str = "magpie"
    mt_base_url: str = "http://127.0.0.1:3425/v1"
    mt_model: str = ""
    mt_free_model: str = "group/transcribe-free"
    asr_provider: str = "bailian"
    magpie_data_dir: str = ""
    magpie_asr_provider: str = "transcribe-bailian"
    magpie_upload_provider: str = "transcribe-upload"
    magpie_base_url: str = "http://127.0.0.1:3425/v1"
    mt_rpm: int = 0
    mt_backends: list | None = None
    mt_routing: dict | None = None
    mt_quality: dict | None = None
    channel: str = "ask"
    poll_seconds: float = 4
    poll_timeout_seconds: float = 6 * 3600
    chinese_first: bool = False
    zh_line_width: int = 44
    other_line_width: int = 84

    def validate(self):
        if self.asr_provider not in {'bailian', 'magpie'}:
            raise ToolError('ASR 接入必须为 bailian 或 magpie。', 'config')
        if self.region != "beijing":
            raise ToolError("仅支持北京工作空间，请检查 data/settings.json。", "config")
        if self.workspace_id:
            self.workspace_id = validate_workspace_id(self.workspace_id)
        if not 1 <= self.mt_workers <= 4:
            raise ToolError("翻译并发数必须为 1～4。", "config")
        if self.channel not in {"ask", "mix", "left", "right"}:
            raise ToolError("声道设置无效。", "config")
        if not 1 <= self.poll_seconds <= 60 or self.poll_timeout_seconds <= 0:
            raise ToolError("轮询设置无效。", "config")
        if not 12 <= self.zh_line_width <= 72 or not 24 <= self.other_line_width <= 160:
            raise ToolError("每行字幕长度设置无效。", "config")


class Config:
    def __init__(self, root: Path | None = None):
        self.root = (root or app_root()).resolve()
        self.data = self.root / "data"
        try:
            self.data.mkdir(parents=True, exist_ok=True)
            probe = self.data / (".write-probe-" + str(os.getpid()))
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as e:
            raise ToolError("项目 data 目录不可写，请将项目移动到可写目录后运行。", "data_readonly") from e
        self.settings = Settings()
        path = self.data / "settings.json"
        if path.exists():
            values = json.loads(path.read_text(encoding="utf-8"))
            self.settings = Settings(**{k: v for k, v in values.items() if k in asdict(self.settings)})
        self.settings.validate()

    def save(self):
        self.settings.validate()
        atomic_json(self.data / "settings.json", asdict(self.settings))

    def mt_settings(self):
        # Resolve only for new tasks/reflow: current choices cannot alter a saved resume.
        overrides = {name: os.environ[name.upper()] for name in
                     ["mt_provider", "mt_base_url", "mt_model", "mt_free_model"] if name.upper() in os.environ}
        return replace(self.settings, **overrides)

    def get_magpie_key(self, base_url=None):
        from .magpie import local_gateway
        from .mt_backend import normalize_mt_url
        base = normalize_mt_url(base_url or self.mt_settings().mt_base_url)
        env = os.environ.get('MAGPIE_API_KEY', '').strip()
        if env:
            return self.validate_key(env)
        path = self.data / 'magpie-key.txt'
        if path.exists():
            try:
                return self.validate_key(path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError):
                raise ToolError('无法读取 magpie 网关凭据。', 'credentials') from None
        if local_gateway(base):
            return 'magpie-transcribe'
        raise ToolError('远程 magpie 需要 MAGPIE_API_KEY 或 data/magpie-key.txt。', 'credentials')

    def configure_magpie(self):
        """Explicit migration; retain the original settings and every credential."""
        previous = self.settings
        selected = replace(previous, mt_provider='magpie', mt_backends=None,
                           mt_routing={'paid_enabled': (previous.mt_routing or {}).get('paid_enabled', True)},
                           mt_base_url=previous.magpie_base_url, mt_model='group/transcribe',
                           mt_free_model='group/transcribe-free')
        selected.validate()
        path = self.data / 'settings.json'
        backup = self.data / 'migrations' / ('magpie-' + uuid.uuid4().hex) / 'settings.json'
        if path.exists():
            atomic_bytes(backup, path.read_bytes())
        self.settings = selected
        try:
            self.save()
        except Exception:
            self.settings = previous
            raise
        return backup

    def asr_gateway(self, manifest):
        if self.settings.asr_provider != 'magpie':
            return None
        from .magpie import asr_providers
        from .mt_backend import key_fingerprint, normalize_mt_url
        provider, _ = asr_providers(self.settings, manifest['workspace_id'])
        if key_fingerprint(self.validate_key(provider['key'])) != manifest.get('account'):
            raise ToolError('此任务的 magpie 百炼 Key 已变更；不能跨账户复用任务或缓存。', 'account_changed')
        base = normalize_mt_url(self.settings.magpie_base_url)
        return {'base_url': base, 'asr_provider': self.settings.magpie_asr_provider,
                'upload_provider': self.settings.magpie_upload_provider, 'key': self.get_magpie_key(base)}

    def mt_rpm(self):
        value = os.environ.get("MT_RPM", self.settings.mt_rpm)
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        if type(value) is not int or value < 0:
            raise ToolError("mt_rpm 必须为非负整数；0 表示不主动限制。", "mt_config")
        return value

    def get_mt_key(self) -> str:
        env = os.environ.get("OPENAI_API_KEY", "").strip()
        if env:
            return self.validate_key(env)
        try:
            return self.validate_key((self.data / "mt-key.txt").read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            raise ToolError("兼容翻译后端需要 OPENAI_API_KEY 或本机 data/mt-key.txt；不会复用百炼 Key。", "credentials") from error

    def get_qiniu_key(self) -> str:
        env = os.environ.get('QINIU_API_KEY', '').strip()
        if env:
            return self.validate_key(env)
        try:
            return self.validate_key((self.data / 'qiniu-key.txt').read_text(encoding='utf-8'))
        except (OSError, UnicodeError) as error:
            raise ToolError('七牛翻译需要 QINIU_API_KEY 或本机 data/qiniu-key.txt。', 'credentials') from error

    def set_qiniu_key(self, key: str):
        key = self.validate_key(key)
        try:
            atomic_bytes(self.data / 'qiniu-key.txt', key.encode('utf-8'))
        except OSError as error:
            raise ToolError('无法写入 data/qiniu-key.txt，请检查目录权限。', 'credentials') from error

    def set_key(self, key: str):
        key = self.validate_key(key)
        try:
            atomic_bytes(self.data / "key.txt", key.encode("utf-8"))
        except OSError as e:
            raise ToolError("无法写入 data/key.txt，请检查目录权限。", "credentials") from e

    @staticmethod
    def validate_key(key):
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            raise ToolError("API Key 为空或包含空白字符。", "credentials")
        return key

    def get_key(self) -> str:
        if self.settings.asr_provider == 'magpie':
            from .magpie import asr_providers
            provider, _ = asr_providers(self.settings, self.settings.workspace_id)
            # Only read the central key for the existing account fingerprint.
            # Network calls use the gateway's own credential, never this key.
            return self.validate_key(provider['key'])
        env = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if env:
            return self.validate_key(env)
        try:
            return self.validate_key((self.data / 'key.txt').read_text(encoding='utf-8'))
        except (OSError, UnicodeError) as error:
            raise ToolError('请运行 --configure-key 配置百炼 Key，或设置 DASHSCOPE_API_KEY。', 'credentials') from error

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .common import ToolError, atomic_bytes, atomic_json, validate_workspace_id


MT_ENV_SETTINGS = ("mt_base_url", "mt_model", "mt_fallback_model", "mt_review_model",
                   "mt_review_fallback_model", "mt_judge_model", "mt_judge_fallback_model")


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    region: str = "beijing"
    workspace_id: str = ""
    mt_workers: int = 16
    mt_base_url: str = "http://127.0.0.1:3425/v1"
    mt_model: str = ""
    mt_fallback_model: str = "group/transcribe-paid"
    mt_review_model: str = "group/transcribe-review"
    mt_review_fallback_model: str = "group/transcribe-review-paid"
    mt_judge_model: str = "group/transcribe-judge"
    mt_judge_fallback_model: str = "group/transcribe-judge-paid"
    mt_policy_version: int = 1
    magpie_data_dir: str = ""
    mt_quality: dict | None = None
    channel: str = "ask"
    poll_seconds: float = 4
    poll_timeout_seconds: float = 6 * 3600
    chinese_first: bool = False
    zh_line_width: int = 44
    other_line_width: int = 84

    def validate(self):
        if self.region != "beijing":
            raise ToolError("仅支持北京工作空间，请检查 data/settings.json。", "config")
        if self.workspace_id:
            self.workspace_id = validate_workspace_id(self.workspace_id)
        if type(self.mt_workers) is not int or self.mt_workers < 1:
            raise ToolError("翻译并发数必须为正整数。", "config")
        if self.channel not in {"ask", "mix", "left", "right"}:
            raise ToolError("声道设置无效。", "config")
        if not 1 <= self.poll_seconds <= 60 or self.poll_timeout_seconds <= 0:
            raise ToolError("轮询设置无效。", "config")
        if not 12 <= self.zh_line_width <= 72 or not 24 <= self.other_line_width <= 160:
            raise ToolError("每行字幕长度设置无效。", "config")


class Config:
    def __init__(self, root: Path | None = None, *, read_only=False):
        self.root = (root or app_root()).resolve()
        self.data = self.root / "data"
        try:
            if not read_only:
                self.data.mkdir(parents=True, exist_ok=True)
                probe = self.data / (".write-probe-" + str(os.getpid()))
                probe.write_bytes(b"")
                probe.unlink()
        except OSError as e:
            raise ToolError("项目 data 目录不可写，请将项目移动到可写目录后运行。", "data_readonly") from e
        path = self.data / "settings.json"
        values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        names = {field.name for field in fields(Settings)}
        self.settings = Settings(**{k: v for k, v in values.items() if k in names})
        self.settings.validate()

    def save(self):
        self.settings.validate()
        atomic_json(self.data / "settings.json", asdict(self.settings))

    def mt_settings(self):
        # Resolve only for new tasks/reflow: current choices cannot alter a saved resume.
        overrides = {name: os.environ[name.upper()] for name in MT_ENV_SETTINGS if name.upper() in os.environ}
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

    @staticmethod
    def validate_key(key):
        if not isinstance(key, str):
            raise ToolError("API Key 无效。", "credentials")
        key = key.strip()
        if not key or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
            raise ToolError("API Key 为空或包含空白字符。", "credentials")
        return key

    def get_key(self) -> str:
        env = os.environ.get('DASHSCOPE_API_KEY', '').strip()
        if env:
            return self.validate_key(env)
        try:
            return self.validate_key((self.data / 'key.txt').read_text(encoding='utf-8'))
        except (OSError, UnicodeError):
            raise ToolError('请用 --configure-key 配置百炼 Key，或设置 DASHSCOPE_API_KEY。', 'credentials') from None

    def set_key(self, key):
        try:
            atomic_bytes(self.data / 'key.txt', self.validate_key(key).encode('utf-8'))
        except OSError:
            raise ToolError('无法保存百炼 Key，请检查 data 目录权限。', 'credentials') from None

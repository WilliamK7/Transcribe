from __future__ import annotations

import ctypes
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .common import ToolError, atomic_bytes, atomic_json, validate_workspace_id


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    region: str = "beijing"
    workspace_id: str = ""
    mt_workers: int = 4
    channel: str = "ask"
    poll_seconds: float = 4
    poll_timeout_seconds: float = 6 * 3600
    chinese_first: bool = False
    zh_line_width: int = 44
    other_line_width: int = 84

    def validate(self):
        if self.region not in {"beijing", "singapore"}:
            raise ToolError("旧设置中的地域无效，请检查 data/settings.json。", "config")
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


class Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def decrypt_legacy(data: bytes) -> bytes:
    if os.name != "nt":
        raise ToolError("无法读取旧 Windows 加密数据，请在本机重新配置 Key。", "credentials")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ToolError("无法读取旧加密数据，请在原 Windows 用户下恢复或重新配置 Key。", "credentials")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


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

    def set_key(self, key: str):
        key = self.validate_key(key)
        try:
            atomic_bytes(self.data / "key.txt", key.encode("utf-8"))
            (self.data / "key.dpapi").unlink(missing_ok=True)
        except OSError as e:
            raise ToolError("无法写入 data/key.txt 或完成旧密钥迁移，请检查目录权限。", "credentials") from e

    @staticmethod
    def validate_key(key):
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            raise ToolError("API Key 为空或包含空白字符。", "credentials")
        return key

    def get_key(self) -> str:
        env = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if env:
            return self.validate_key(env)
        plain = self.data / "key.txt"
        legacy = self.data / "key.dpapi"
        try:
            if plain.exists():
                return self.validate_key(plain.read_text(encoding="utf-8"))
            if not legacy.exists():
                raise ToolError("请在本机运行 --configure 或 --configure-key（也可设置 DASHSCOPE_API_KEY）。", "credentials")
            key = self.validate_key(decrypt_legacy(legacy.read_bytes()).decode("utf-8"))
            self.set_key(key)
            return key
        except (OSError, UnicodeError, ToolError) as e:
            raise ToolError("无法读取或迁移本机 Key；旧文件已保留时可重试，也可运行 --configure-key 重新输入。", "credentials") from e

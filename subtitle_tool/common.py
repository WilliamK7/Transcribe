from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

ASR_MODEL = "qwen3-asr-flash-filetrans"
MT_MODEL = "qwen-mt-flash"
NORMALIZER_VERSION = 3
PROCESSING_VERSION = 2
SUBTITLE_VERSION = 2
DISPLAY_TIMING_VERSION = 2
TRANSLATION_STRATEGY_VERSION = 3
# Qwen-MT's 8192-token input limit includes translation memory and template tokens.
# UTF-8 bytes upper-bound byte-tokenized text; reserve additional template capacity.
MT_INPUT_BUDGET = 6000
MT_TEMPLATE_RESERVE = 512
MAX_LAYOUT_ROUNDS = 2
MAX_UPLOAD_BYTES = 1_000_000_000
MAX_ASR_SECONDS = 12 * 3600
API_ROOTS = {
    "beijing": "https://dashscope.aliyuncs.com",
}


def validate_workspace_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value):
        raise ToolError("Workspace ID 必须是 1～63 位域名标签（字母、数字、内部连字符），不能填写完整 URL。请运行 --configure。", "workspace_id")
    return value.lower()


def connection_identity(manifest: dict) -> dict:
    """Missing fields identify pre-CLI public-endpoint tasks, never the current settings."""
    region = manifest.get("region", "beijing")
    if region != "beijing":
        raise ToolError("旧新加坡任务数据已保留；本版只支持北京，不能自动切换地域恢复。", "legacy_region")
    mode = manifest.get("access_mode", "legacy_public")
    workspace = manifest.get("workspace_id", "")
    if mode == "workspace":
        workspace = validate_workspace_id(workspace)
    elif mode != "legacy_public" or workspace:
        raise ToolError("任务的工作空间／接入方式记录不一致。", "cache_mismatch")
    return {"region": region, "workspace_id": workspace, "access_mode": mode}


class ToolError(Exception):
    def __init__(self, message, code="local_error", request_id=""):
        super().__init__(redact(str(message)))
        self.code = re.sub(r"[^A-Za-z0-9_.:-]", "_", redact(str(code)))[:160]
        self.request_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", redact(str(request_id)))[:200]


class Cancelled(ToolError):
    def __init__(self):
        super().__init__("已停止本地处理；已提交的云端任务仍可能计费。", "cancelled")


class NeedsReview(ToolError):
    pass


class SubmissionUnknown(ToolError):
    def __init__(self, request_id=""):
        super().__init__("提交结果未知，可能已创建云端任务。请绑定已知 task_id，或明确确认重新提交（可能重复计费）。",
                         "submission_unknown", request_id)


def check_cancel(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled()


def redact(value: str) -> str:
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", value)
    value = re.sub(r"\bsk-[\w-]+", "[已隐藏密钥]", value)
    value = re.sub(r"(?:https?://|oss://)[^\s\"'<>]+", "[已隐藏链接]", value)
    return value[:1600]


def atomic_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def fingerprint(path: Path, cancel: threading.Event) -> dict:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while data := f.read(4 * 1024 * 1024):
            check_cancel(cancel)
            digest.update(data)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ToolError("读取期间源文件发生变化，请重新入队。", "source_changed")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()}


def output_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve())).casefold()

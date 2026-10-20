from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .common import ToolError, output_key


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def prepare_intent(path, content, overwrite):
    path = path.resolve()
    original = file_hash(path) if path.exists() else None
    if original is not None and not overwrite:
        raise ToolError("同名 SRT 已存在，已跳过。", "exists")
    return {"target": str(path), "output_key": output_key(path),
            "expected_srt_sha256": hashlib.sha256(content).hexdigest(), "overwrite": bool(overwrite),
            "original_sha256": original,
            "backup": str(path.with_name(path.name + f".bak.{time.time_ns()}")) if original is not None else ""}


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)


def validate_intent(path, intent):
    try:
        if (not isinstance(intent, dict) or intent.get("target") != str(path.resolve()) or
                intent.get("output_key") != output_key(path) or type(intent.get("overwrite")) is not bool or
                not _valid_hash(intent.get("expected_srt_sha256"))):
            raise ValueError()
        original, backup = intent["original_sha256"], intent["backup"]
        if original is None:
            if backup != "":
                raise ValueError()
        else:
            if not _valid_hash(original) or not intent["overwrite"] or not isinstance(backup, str):
                raise ValueError()
            destination = Path(backup)
            if (str(destination.resolve()) != backup or destination.parent != path.resolve().parent or
                    not destination.name.startswith(path.name + ".bak.")):
                raise ValueError()
    except (KeyError, ValueError, TypeError, OSError) as error:
        raise ToolError("输出发布意图与保存目标不一致；文件已保留。", "cache_mismatch") from error


def _conflict():
    return ToolError("发布目标或预定备份已被外部修改；全部文件已保留，请使用其他 --output-dir。", "output_conflict")


def inspect_intent(path, intent):
    """Return true only for already published bytes; otherwise verify retry preconditions."""
    validate_intent(path, intent)
    backup = Path(intent["backup"]) if intent["backup"] else None
    if backup is not None and backup.exists() and file_hash(backup) != intent["original_sha256"]:
        raise _conflict()
    current = file_hash(path) if path.exists() else None
    if current == intent["expected_srt_sha256"]:
        return True
    if current != intent["original_sha256"]:
        raise _conflict()
    return False


def _create_exclusive(temp, destination):
    try:
        os.link(temp, destination)
    except FileExistsError:
        raise
    except OSError:
        if os.name != "nt":
            raise
        # Windows rename is exclusive, including volumes without hard-link support.
        os.rename(temp, destination)


def backup_original(path, intent):
    """Create the reserved backup only after its entire content has been synced."""
    if not intent["backup"]:
        return ""
    backup = Path(intent["backup"])
    if backup.exists():
        if file_hash(backup) != intent["original_sha256"]:
            raise _conflict()
        return str(backup)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".backup-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as target, path.open("rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        if file_hash(temp) != intent["original_sha256"]:
            raise _conflict()
        try:
            _create_exclusive(temp, backup)
        except FileExistsError:
            if file_hash(backup) != intent["original_sha256"]:
                raise _conflict()
        return str(backup)
    finally:
        temp.unlink(missing_ok=True)


def write_srt(path: Path, content: bytes, overwrite=False, *, intent=None):
    """Publish SRT bytes atomically, reusing the saved backup on retry."""
    path = path.resolve()
    intent = prepare_intent(path, content, overwrite) if intent is None else intent
    published = inspect_intent(path, intent)
    if hashlib.sha256(content).hexdigest() != intent["expected_srt_sha256"]:
        raise ToolError("渲染结果与已保存的发布内容不一致；原目标已保留。", "cache_mismatch")
    if published:
        return intent["backup"] if intent["backup"] and Path(intent["backup"]).is_file() else ""
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        backup = backup_original(path, intent)
        # Recheck after backup creation; a retry must not overwrite an external edit.
        if inspect_intent(path, intent):
            return backup
        if intent["original_sha256"] is not None:
            os.replace(temp, path)
        else:
            try:
                _create_exclusive(temp, path)
            except FileExistsError as error:
                raise _conflict() from error
        return backup
    finally:
        temp.unlink(missing_ok=True)

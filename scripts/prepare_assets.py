"""Prepare the pinned, hash-verified media tools for local CLI use."""
from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = json.loads((ROOT / "scripts/release-assets.json").read_text(encoding="utf-8"))


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download_tools():
    asset = ASSETS["ffmpeg"]
    downloads = ROOT / ".build/downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / ("ffmpeg-" + asset["version"] + ".zip")
    if not archive.exists():
        temporary = archive.with_suffix(".download")
        try:
            # urllib uses OS-backed default TLS verification; never disable certificate checks.
            urllib.request.urlretrieve(asset["url"], temporary)
            if sha(temporary) != asset["sha256"]:
                raise RuntimeError("FFmpeg archive hash mismatch")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if sha(archive) != asset["sha256"]:
        raise RuntimeError("FFmpeg archive hash mismatch")
    target = ROOT / "tools"
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as files:
        for name in ["ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"]:
            candidates = [n for n in files.namelist() if n.endswith("/" + name)]
            if len(candidates) != 1:
                raise RuntimeError("Expected FFmpeg asset missing: " + name)
            output = target / name
            # Explicit allowlist extraction prevents ZIP path traversal.
            with files.open(candidates[0]) as source, output.open("wb") as destination:
                shutil.copyfileobj(source, destination)
    return archive



if __name__ == "__main__":
    print(download_tools())

from __future__ import annotations

import platform
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import Config, Settings
from .instance import ProcessLock
from .media import Media, run_process
from .reports import ReportWriter
from .subtitles import make_fragment, normalize, render_srt, write_srt


def self_test(config: Config, report: Path):
    """Isolated real local checks, zero cloud calls and no access to the user's Key."""
    writer = ReportWriter(config, report, self_test=True)
    writer.validate()
    versions = {}
    media = Media(config.root, threading.Event())
    for name in ["ffmpeg", "ffprobe"]:
        out, _ = run_process([media.executable(name), "-version"], media.cancel, 15)
        versions[name] = out.decode("utf-8", errors="replace").splitlines()[0]
    with tempfile.TemporaryDirectory(prefix="selftest-", dir=config.data) as directory:
        temporary = Path(directory)
        isolated = Config(temporary)
        isolated.set_key("diagnostic-placeholder-not-an-api-key")
        assert (isolated.data / "key.txt").read_bytes() == b"diagnostic-placeholder-not-an-api-key"
        first, second = ProcessLock(isolated.data), ProcessLock(isolated.data)
        try:
            assert first.acquire()
            assert not second.acquire()
        finally:
            first.close()
            second.close()
        sample = temporary / "中文 多点.片段 & (1).m4a"
        run_process([media.executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=48000", "-c:a", "aac", sample], media.cancel)
        info = media.probe(sample)
        track, _ = media.select(info)
        plan, segments = media.prepare(sample, temporary / "cache", info, track, "ask")
        assert plan["method"] == "copy" and len(segments) == 1
        raw = {"transcripts": [{"channel_id": 0, "sentences": [
            {"begin_time": 100, "end_time": 1200, "text": "中文本地验证。", "language": "zh"}]}]}
        sources, _ = normalize(raw, plan["audio_offset_ms"], "0", preserve_sentences=True)
        cues = [make_fragment(source) for source in sources]
        srt = render_srt(cues, Settings())
        write_srt(sample.with_suffix(".srt"), srt)
        assert sample.with_suffix(".srt").read_bytes() == srt
    result = {"timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "version": __version__, "kind": "local_cli_smoke_only", "passed": True, "cloud_calls": 0,
              "python": platform.python_version(), "architecture": platform.machine(),
              "platform": platform.platform(), "media_tools": versions,
              "checks": ["隔离目录原子明文 Key 保存", "任务库独占锁",
                         "固定 ffprobe 检查合成媒体", "AAC 音轨复制及时间轴校验", "特殊字符路径",
                         "规范化和同名 UTF-8 CRLF SRT 原子写入"]}
    writer.write(result)
    return result

from __future__ import annotations

import json
import hashlib
import math
import os
import subprocess
import threading
import wave
import tempfile
from fractions import Fraction
from pathlib import Path

from .common import (MAX_ASR_SECONDS, MAX_UPLOAD_BYTES, PROCESSING_VERSION, NeedsReview,
                     ToolError, check_cancel)


def run_process(args, cancel: threading.Event, timeout=None, stdout=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    check_cancel(cancel)
    process = subprocess.Popen([str(a) for a in args], stdin=subprocess.DEVNULL, stdout=stdout or subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=flags, shell=False)
    # communicate timeout drains both pipes without deadlock, with bounded cancellation latency.
    elapsed = 0
    while True:
        try:
            out, err = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            elapsed += 0.25
            if cancel.is_set() or (timeout and elapsed >= timeout):
                process.kill()
                process.communicate()
                check_cancel(cancel)
                raise ToolError("媒体工具执行超时。", "media_timeout")
        except BaseException:
            process.kill()
            process.communicate()
            raise
    if process.returncode:
        # FFmpeg may print the full input; logs retain only generic error plus exit code.
        raise ToolError(f"媒体工具失败（退出码 {process.returncode}）；请确认格式、音轨、磁盘空间和文件权限。", "media")
    return out, err


class Media:
    def __init__(self, root: Path, cancel: threading.Event):
        self.root, self.cancel = root, cancel

    def executable(self, name):
        path = self.root / "tools" / (name + (".exe" if os.name == "nt" else ""))
        if not path.is_file():
            raise ToolError("缺少项目媒体工具，请运行 scripts/setup.ps1 准备固定 FFmpeg。", "tools_missing")
        return path

    def probe(self, path: Path):
        out, _ = run_process([self.executable("ffprobe"), "-v", "error", "-show_format", "-show_streams",
                              "-of", "json", path], self.cancel, 60)
        body = json.loads(out)
        tracks = [s for s in body.get("streams", []) if s.get("codec_type") == "audio"]
        for track in tracks:
            track["container_format"] = body.get("format", {}).get("format_name", "")
        if not tracks:
            raise ToolError("媒体没有音轨。", "no_audio")
        duration = float(body.get("format", {}).get("duration", 0))
        if duration <= 0 or not math.isfinite(duration):
            raise ToolError("无法确定媒体时长。", "media_duration")
        starts = [float(s["start_time"]) for s in body.get("streams", []) if s.get("start_time") is not None]
        origin = float(body.get("format", {}).get("start_time", min(starts) if starts else 0))
        return {"duration_ms": round(duration * 1000), "origin_ms": round(origin * 1000), "tracks": tracks}

    def select(self, info, track_index=None, channel="ask"):
        tracks = info["tracks"]
        if track_index is None:
            defaults = [t for t in tracks if t.get("disposition", {}).get("default") == 1]
            if len(tracks) == 1:
                selected = tracks[0]
            elif len(defaults) == 1:
                selected = defaults[0]
            else:
                raise NeedsReview("存在多个音轨，请用 --review 或 --resume 配合 --track 选择。", "audio_track")
        else:
            selected = next((t for t in tracks if t["index"] == track_index), None)
            if selected is None:
                raise ToolError("选定音轨不存在。", "audio_track")
        channels = int(selected.get("channels", 1))
        if channels > 1 and channel == "ask":
            raise NeedsReview("音轨包含多个声道，请确认混音或只选左／右声道，避免混入独立语言。", "audio_channel")
        if channel == "right" and channels < 2:
            raise ToolError("单声道音轨没有右声道。", "audio_channel")
        # Use the container's playback origin, never remove leading silence.
        offset = round(float(selected.get("start_time", info["origin_ms"] / 1000)) * 1000) - info["origin_ms"]
        return selected["index"], offset

    def packet_timeline(self, source, track, directory):
        """Scan timestamps on disk, so long files do not keep all packets in memory."""
        rate = int(track.get("sample_rate", 0))
        time_base = Fraction(track.get("time_base", "0"))
        if rate <= 0 or time_base <= 0:
            raise ToolError("音轨缺少可靠时间基准。", "media_timeline")
        with tempfile.TemporaryFile(dir=directory) as packets:
            run_process([self.executable("ffprobe"), "-v", "error", "-select_streams", str(track["index"]),
                         "-show_packets", "-show_entries", "packet=pts,duration:packet_side_data=skip_samples,discard_padding",
                         "-of", "compact=p=0", source], self.cancel, stdout=packets)
            packets.seek(0)
            count, skipped, padding = 0, 0, 0
            previous_padding = 0
            first, end, previous = None, None, None
            continuous = True
            tolerance = max(Fraction(2, rate), time_base * 2)
            for line in packets:
                check_cancel(self.cancel)
                values = dict(field.rsplit("=", 1) for field in line.decode("ascii").strip().split("|") if "=" in field)
                if "pts" not in values:
                    continue
                try:
                    pts = int(values["pts"]) * time_base
                    duration = int(values["duration"]) * time_base
                    skip = int(next((v for k, v in values.items() if k.endswith("skip_samples")), "0"))
                    pad = int(next((v for k, v in values.items() if k.endswith("discard_padding")), "0"))
                except (ValueError, KeyError) as e:
                    raise ToolError("音轨包时间戳不完整，需解码校正。", "media_timeline") from e
                if duration < 0 or skip < 0 or pad < 0:
                    raise ToolError("音轨包时间戳异常，需解码校正。", "media_timeline")
                if previous is not None and abs(pts - previous) > tolerance:
                    continuous = False
                if skip and count > 0:
                    continuous = False
                if previous_padding:
                    continuous = False
                previous_padding = pad
                if first is None:
                    first = pts + Fraction(skip, rate)
                    if track.get("codec_name") == "vorbis":
                        # The first Vorbis packet primes the overlap decoder; it emits no samples.
                        first = pts + duration
                # Ogg granule positions already trim the final packet duration. MP3 durations do not.
                ogg = track.get("container_format") == "ogg"
                end = pts + duration - (0 if ogg else Fraction(pad, rate))
                previous = pts + duration
                count += 1
                skipped += skip
                padding += pad
        if first is None or end <= first:
            raise ToolError("无法取得音轨的有效时间范围。", "media_timeline")
        return {"begin_seconds": float(first), "end_seconds": float(end), "duration_seconds": float(end - first),
                "packets": count, "continuous": continuous, "skip_samples": skipped,
                "discard_padding": padding, "sample_rate": rate, "time_base": str(time_base)}

    def copy_audio(self, source, target, track, begin_seconds):
        temp = target.with_name(target.stem + ".partial" + target.suffix)
        try:
            run_process([self.executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-copyts", "-i", source,
                         "-map", f"0:{track}", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-c:a", "copy",
                         "-avoid_negative_ts", "disabled", "-output_ts_offset", f"{-begin_seconds:.9f}", temp], self.cancel)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def encode_flac(self, source, target):
        temp = target.with_suffix(".partial.flac")
        try:
            run_process([self.executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", source,
                         "-map_metadata", "-1", "-c:a", "flac", temp], self.cancel)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def audio_identity(self, path):
        digest = hashlib.sha256()
        with path.open("rb") as audio:
            while block := audio.read(4 * 1024 * 1024):
                check_cancel(self.cancel)
                digest.update(block)
        return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}

    def prepare(self, source, directory, info, track_index, channel, limit_bytes=MAX_UPLOAD_BYTES,
                *, force_method=None):
        """Prefer lossless stream copy; every fallback uses the existing corrected PCM timeline."""
        directory.mkdir(parents=True, exist_ok=True)
        track = next(t for t in info["tracks"] if t["index"] == track_index)
        _, offset = self.select(info, track_index, channel)
        codec = track.get("codec_name", "")
        suffix = {"aac": ".m4a", "mp3": ".mp3", "flac": ".flac", "opus": ".ogg", "vorbis": ".ogg"}.get(codec)
        reason = "声道处理或编码格式需要转换"
        timeline = None
        try:
            timeline = self.packet_timeline(source, track, directory)
            offset = round(timeline["begin_seconds"] * 1000) - info["origin_ms"]
        except ToolError as error:
            if error.code != "media_timeline":
                raise
            reason = "时间戳缺少可靠信息，使用解码校正"
        eligible = (suffix and timeline and timeline["continuous"] and
                    (int(track.get("channels", 1)) == 1 or channel == "left") and
                    timeline["duration_seconds"] <= MAX_ASR_SECONDS)
        if force_method == "copy" and not eligible:
            raise ToolError("原复制方案已无法验证，不能改变已有任务的识别输入。", "cache_mismatch")
        if eligible and force_method != "flac":
            target = directory / ("audio" + suffix)
            try:
                self.copy_audio(source, target, track_index, timeline["begin_seconds"])
                extracted = self.probe(target)
                extracted_track = extracted["tracks"][0]
                copied = self.packet_timeline(target, extracted_track, directory)
                tolerance = max(2 / timeline["sample_rate"], float(Fraction(timeline["time_base"])) * 2,
                                float(Fraction(copied["time_base"])) * 2)
                reliable = (copied["continuous"] and copied["packets"] == timeline["packets"] and
                            copied["skip_samples"] == timeline["skip_samples"] and
                            copied["discard_padding"] == timeline["discard_padding"] and
                            abs(copied["duration_seconds"] - timeline["duration_seconds"]) <= tolerance and
                            abs(copied["begin_seconds"] - extracted["origin_ms"] / 1000) <= tolerance and
                            extracted_track.get("codec_name") == codec and
                            extracted_track.get("channels") == track.get("channels") and
                            copied["sample_rate"] == timeline["sample_rate"])
                if not reliable:
                    reason = "复制后时间轴或编码延迟不一致，使用解码校正"
                elif target.stat().st_size > limit_bytes:
                    reason = "压缩音轨超过上传容量，使用精确分段"
                else:
                    segment = {"id": "0000", "audio": target.name, "offset_ms": 0,
                               "duration_ms": round(timeline["duration_seconds"] * 1000), "codec": codec,
                               "container": suffix[1:], **self.audio_identity(target)}
                    return {"version": PROCESSING_VERSION, "method": "copy", "track": track_index,
                            "channel": channel, "codec": codec, "container": suffix[1:], "audio_offset_ms": offset,
                            "source_timeline": timeline, "extracted_timeline": copied, "limit_bytes": limit_bytes,
                            "reason": "已验证连续时间轴和编码延迟，直接复制压缩音轨"}, [segment]
            except ToolError as error:
                if error.code not in {"media", "media_timeline", "media_duration"}:
                    raise
                reason = "音轨与目标容器不兼容，使用解码校正"
            target.unlink(missing_ok=True)
            if force_method == "copy":
                raise ToolError("恢复复制音轨与原方案不同，不能更改识别输入。", "cache_mismatch")
        elif timeline and not timeline["continuous"]:
            reason = "时间轴不连续，使用解码校正保留间隙"
        elif timeline and timeline["duration_seconds"] > MAX_ASR_SECONDS:
            reason = "音轨超过单段时长，使用精确分段"
        pcm = directory / "audio-pcm.wav"
        self.extract(source, pcm, track_index, channel)
        try:
            with wave.open(str(pcm), "rb") as reader:
                frames, rate = reader.getnframes(), reader.getframerate()
            target = directory / "audio.flac"
            if frames / rate <= MAX_ASR_SECONDS:
                self.encode_flac(pcm, target)
                if target.stat().st_size <= limit_bytes:
                    return {"version": PROCESSING_VERSION, "method": "flac", "track": track_index,
                            "channel": channel, "codec": "flac", "container": "flac", "audio_offset_ms": offset,
                            "source_timeline": timeline, "limit_bytes": limit_bytes, "reason": reason}, [
                        {"id": "0000", "audio": target.name, "offset_ms": 0, "duration_ms": round(frames * 1000 / rate),
                         "start_frame": 0, "sample_count": frames, "sample_rate": rate, "codec": "flac",
                         "container": "flac", **self.audio_identity(target)}]
                target.unlink(missing_ok=True)
            # PCM capacity is conservative. Check each actual encoded file as well.
            cap = limit_bytes
            for attempt in range(4):
                chunks = self.split(pcm, directory, cap)
                segments = []
                for chunk in chunks:
                    wav = directory / chunk["audio"]
                    target = wav.with_suffix(".flac")
                    self.encode_flac(wav, target)
                    segments.append({**chunk, "audio": target.name, "codec": "flac", "container": "flac",
                                     **self.audio_identity(target)})
                if all(s["bytes"] <= limit_bytes for s in segments):
                    for chunk in chunks:
                        (directory / chunk["audio"]).unlink(missing_ok=True)
                    return {"version": PROCESSING_VERSION, "method": "flac", "track": track_index,
                            "channel": channel, "codec": "flac", "container": "flac", "audio_offset_ms": offset,
                            "source_timeline": timeline, "limit_bytes": limit_bytes, "pcm_split_limit": cap,
                            "reason": reason + "；PCM 精确切分后编码 FLAC"}, segments
                for segment in segments:
                    (directory / segment["audio"]).unlink(missing_ok=True)
                cap = int(cap * 0.8)
            raise ToolError("分段编码后仍超过上传容量。", "upload_size")
        finally:
            pcm.unlink(missing_ok=True)

    def extract(self, source: Path, path: Path, track, channel):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".partial.wav")
        filters = []
        if channel in {"left", "right"}:
            filters.append("pan=mono|c0=c" + ("0" if channel == "left" else "1"))
        filters.extend(["asetpts=PTS-STARTPTS", "aresample=16000:async=1000:first_pts=0"])
        try:
            run_process([self.executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", source,
                         "-map", f"0:{track}", "-vn", "-af", ",".join(filters), "-ac", "1", "-ar", "16000",
                         "-c:a", "pcm_s16le", "-f", "wav", temp], self.cancel)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def split(self, path: Path, directory: Path, limit_bytes=MAX_UPLOAD_BYTES):
        """Exact PCM frame slicing, no overlap, optional nearby silence. Offsets are integer ms."""
        with wave.open(str(path), "rb") as audio:
            rate, width, channels = audio.getframerate(), audio.getsampwidth(), audio.getnchannels()
            total = audio.getnframes()
            if (rate, width, channels) != (16000, 2, 1):
                raise ToolError("提取音频不是预期的 PCM16／16kHz／单声道。", "media")
            # Conservative allowance for WAV headers and upload policy caps.
            max_frames = min(int(MAX_ASR_SECONDS * rate), int((limit_bytes - 4096) / (width * channels)))
            max_frames = max_frames // (rate // 1000) * (rate // 1000)
            if max_frames < rate:
                raise ToolError("上传凭证容量不足以容纳音频分段。", "upload_size")
            if total <= max_frames:
                return [{"id": "0000", "audio": path.name, "offset_ms": 0,
                         "duration_ms": round(total * 1000 / rate), "start_frame": 0,
                         "sample_count": total, "sample_rate": rate}]
            segments, start = [], 0
            while start < total:
                check_cancel(self.cancel)
                end = min(total, start + max_frames)
                if end < total:
                    # Search the final ten seconds for >=200ms of quiet, without removing any samples.
                    window_start = max(start, end - 10 * rate)
                    audio.setpos(window_start)
                    window = audio.readframes(end - window_start)
                    import array
                    samples = array.array("h", window)
                    block, quiet, best = rate // 10, 0, None
                    for at in range(0, len(samples) - block + 1, block):
                        peak = max(abs(s) for s in samples[at:at + block])
                        quiet = quiet + 1 if peak < 250 else 0
                        if quiet >= 2:
                            best = window_start + at + block // 2
                    if best and best > start + 10 * rate:
                        end = best // (rate // 1000) * (rate // 1000)
                segment_id = f"{len(segments):04}"
                target = directory / ("audio-" + segment_id + ".wav")
                temp = target.with_suffix(".partial.wav")
                try:
                    audio.setpos(start)
                    with wave.open(str(temp), "wb") as writer:
                        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
                        remaining = end - start
                        while remaining:
                            check_cancel(self.cancel)
                            amount = min(rate * 10, remaining)
                            writer.writeframesraw(audio.readframes(amount))
                            remaining -= amount
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
                segments.append({"id": segment_id, "audio": target.name,
                                 "offset_ms": start * 1000 // rate, "duration_ms": round((end - start) * 1000 / rate),
                                 "start_frame": start, "sample_count": end - start, "sample_rate": rate})
                start = end
            return segments

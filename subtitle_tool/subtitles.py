from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .common import MT_MODEL, ToolError

SUPPORTED = {"zh", "yue", "en", "ja", "de", "ko", "ru", "fr", "pt", "ar", "it", "es", "hi",
             "id", "th", "tr", "uk", "vi", "cs", "da", "fil", "fi", "is", "ms", "no", "pl", "sv"}
WORD_LANGS = {"zh", "yue", "en", "ja", "ko", "de", "fr", "es", "it", "pt", "ru"}
ZH = re.compile(r"[\u3400-\u9fff\U00020000-\U0002ffff]")
KANA = re.compile(r"[\u3040-\u30ff]")
HANGUL = re.compile(r"[\uac00-\ud7af]")
LATIN = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
BRANDS = {"windows", "python", "ffmpeg", "ffprobe", "qwen", "chatgpt", "openai", "github",
          "iphone", "ipad", "android", "bailian", "linux", "macos", "sql", "javascript", "api"}
CHINESE_MARKERS = re.compile(r"的|了|我们|你们|这个|欢迎|今天|你好|一个|可以|需要|使用|然后|请|谢谢|中文")
FOREIGN_FUNCTIONS = {"the", "and", "is", "are", "was", "you", "your", "please", "turn", "on", "off",
                     "with", "have", "this", "that", "what", "where", "why", "when", "how", "from", "to", "for"}


def route(text: str, language: str, neighbors=()) -> tuple[str, str]:
    """Conservative local rules. No Latin=>English default; uncertainty requires explicit review."""
    language = (language or "").lower().strip()
    chinese = len(ZH.findall(text))
    words = LATIN.findall(text)
    substantive = [w for w in words if w.lower() not in BRANDS and
                   (not w.isupper() or w.lower() in FOREIGN_FUNCTIONS or len(w) >= 5)]
    if KANA.search(text):
        if language in {"zh", "yue"}:
            return "uncertain", "中文标签与假名冲突"
        return "translate", "日文文本／标签"
    if HANGUL.search(text):
        if language in {"zh", "yue"}:
            return "uncertain", "中文标签与韩文冲突"
        return "translate", "韩文文本／标签"
    if language in {"zh", "yue"}:
        if not chinese and sum(len(w) for w in words) >= 10:
            return "uncertain", "中文标签与外文文本冲突"
        if len(substantive) >= 4 and sum(len(w) for w in substantive) >= 16:
            return "translate", "句内含实质外语，保留整句原文"
        return "original", "中文／粤语；品牌和缩写不触发翻译"
    if language in SUPPORTED:
        if chinese >= 4 and not substantive and language != "ja":
            return "uncertain", "外语标签与汉字文本冲突"
        if len(re.sub(r"\W", "", text)) < 3 and not any(n == language for n in neighbors):
            return "uncertain", "短句需结合语种确认"
        return "translate", "ASR 外语标签"
    if chinese >= 4 and chinese / max(1, chinese + sum(len(w) for w in words)) >= 0.65:
        if len(substantive) >= 4:
            return "translate", "中文句内实质外语"
        if CHINESE_MARKERS.search(text) or any(n in {"zh", "yue"} for n in neighbors):
            return "original", "本地中文文字规则和上下文"
    if re.search(r"[\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f\u0400-\u04ff]", text):
        return "translate", "本地非中文文字规则；MT 自动源语言"
    return "uncertain", "语种缺失／不支持／文字过短；不默认英文"


def clean_text(text):
    if not isinstance(text, str):
        raise ToolError("识别文本字段无效。", "response_schema")
    return re.sub(r"\s+", " ", text.replace("\x00", "")).strip()


def split_sentence(sentence: dict) -> list[tuple[int, int, str]]:
    begin, end, text = sentence["begin_time"], sentence["end_time"], clean_text(sentence["text"])
    words = sentence.get("words")
    # Sentence timing is retained unless valid word timings account for the whole original text.
    if end - begin <= 7000 or sentence.get("language") not in WORD_LANGS or not isinstance(words, list) or not words:
        return [(begin, end, text)]
    pieces = []
    last = begin
    for word in words:
        if not isinstance(word, dict):
            return [(begin, end, text)]
        b, e = word.get("begin_time"), word.get("end_time")
        if type(b) is not int or type(e) is not int or b < last or e <= b or e > end:
            return [(begin, end, text)]
        pieces.append((b, e, clean_text(word.get("text", "")) + word.get("punctuation", "")))
        last = e
    compact = lambda x: re.sub(r"\s", "", x)
    if compact("".join(p[2] for p in pieces)) != compact(text):
        return [(begin, end, text)]
    result, group = [], []
    for index, piece in enumerate(pieces):
        group.append(piece)
        gap = index + 1 < len(pieces) and pieces[index + 1][0] - piece[1] >= 400
        punct = bool(re.search(r"[。！？.!?;；]$", piece[2]))
        if (piece[1] - group[0][0] >= 3000 and (gap or punct)) or index == len(pieces) - 1:
            joiner = "" if sentence.get("language") in {"zh", "yue", "ja"} else " "
            result.append((group[0][0], group[-1][1], joiner.join(p[2] for p in group)))
            group = []
    return result


def prepare_sentence_timings(sentences, duration_ms, segment_id):
    """Zero-length ASR points get display timing, never invented word alignment."""
    records, warnings = [], []
    for index, sentence in enumerate(sentences):
        if not isinstance(sentence, dict):
            raise ToolError("识别句子结构无效。", "response_schema")
        b, e = sentence.get("begin_time"), sentence.get("end_time")
        if type(b) is not int or type(e) is not int or b < 0 or e < b:
            raise ToolError(f"第 {index + 1} 句时间戳异常；原始结果已保留，需修复后恢复。", "timeline")
        records.append({**sentence, "_indices": [index], "_adjustments": []})
    at = 0
    while at < len(records):
        sentence = records[at]
        b, e = sentence["begin_time"], sentence["end_time"]
        if e > b:
            at += 1
            continue
        index = sentence["_indices"][0]
        text = clean_text(sentence.get("text"))
        words = sentence.get("words")
        compact = lambda value: re.sub(r"\s", "", value)
        corroborated = (0 < len(text) <= 12 and sentence.get("language") in WORD_LANGS and
                        isinstance(words, list) and bool(words) and all(isinstance(w, dict) and
                        type(w.get("begin_time")) is int and type(w.get("end_time")) is int and
                        w["begin_time"] == b and w["end_time"] == b and isinstance(w.get("text"), str) and
                        isinstance(w.get("punctuation", ""), str) for w in words))
        if corroborated:
            corroborated = compact("".join(w["text"] + w.get("punctuation", "") for w in words)) == compact(text)
        if not corroborated:
            raise ToolError(f"第 {index + 1} 句零时长且缺少一致的短句时间点；原始结果已保留。", "timeline")
        previous = records[at - 1] if at else None
        following = records[at + 1] if at + 1 < len(records) else None
        display_end = b + 1000
        if following:
            display_end = min(display_end, following["begin_time"])
        if duration_ms is not None:
            display_end = min(display_end, duration_ms)
        adjustment = {"asr_sentence_index": index, "anchor_ms": b, "original_begin_ms": b,
                      "original_end_ms": e, "kind": "point_display_window"}
        neighbor = None
        direction = None
        if display_end - b < 400:
            for candidate, side in [(previous, "previous"), (following, "next")]:
                if not candidate or candidate.get("language") != sentence.get("language"):
                    continue
                cb, ce = candidate["begin_time"], candidate["end_time"]
                if ce <= cb:
                    continue
                distance = max(cb - b, b - ce, 0)
                if distance <= 400:
                    neighbor, direction = candidate, side
                    break
        if neighbor:
            joiner = "" if sentence.get("language") in {"zh", "yue", "ja"} else " "
            first, second = (neighbor, sentence) if direction == "previous" else (sentence, neighbor)
            adjustment.update(kind="merged_asr_point", direction=direction)
            merged = {**neighbor, "begin_time": min(b, neighbor["begin_time"]),
                      "end_time": max(b, neighbor["end_time"]),
                      "text": clean_text(first["text"]) + joiner + clean_text(second["text"]),
                      "words": None, "_indices": first["_indices"] + second["_indices"],
                      "_adjustments": first["_adjustments"] + second["_adjustments"] + [adjustment]}
            if direction == "previous":
                records[at - 1] = merged
                records.pop(at)
            else:
                records[at + 1] = merged
                records.pop(at)
            warnings.append(f"分段 {segment_id} 第 {index + 1} 句零时长，合并相邻同语种句；保留文字和已有句级边界，未构造词级时间")
            continue
        if display_end <= b:
            raise ToolError(f"第 {index + 1} 句零时长且没有可靠显示区间，未跨语种合并。", "timeline")
        sentence["end_time"] = display_end
        sentence["words"] = None
        sentence["_adjustments"].append(adjustment)
        warnings.append(f"分段 {segment_id} 第 {index + 1} 句零时长，保留 ASR 时间点并补 {display_end - b}ms 显示区间；该区间不代表词级语音时长")
        at += 1
    return records, warnings


def normalize(raw: dict, offset_ms: int, segment_id: str, *, duration_ms=None) -> tuple[list[dict], list[str]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("transcripts"), list):
        raise ToolError("Qwen3 结果缺少 transcripts 数组，不能套用其他 ASR 结构。", "response_schema")
    cues, warnings = [], []
    sentences = []
    for transcript in raw["transcripts"]:
        if not isinstance(transcript, dict):
            raise ToolError("识别音轨结构无效。", "response_schema")
        if transcript.get("channel_id", 0) != 0:
            raise ToolError("结果包含未请求的通道。", "response_schema")
        if not isinstance(transcript.get("sentences"), list):
            raise ToolError("结果缺少 sentences 数组。", "response_schema")
        sentences.extend(transcript["sentences"])
    records, timing_warnings = prepare_sentence_timings(sentences, duration_ms, segment_id)
    warnings.extend(timing_warnings)
    languages = [s.get("language", "") for s in sentences]
    for sentence in records:
        index = sentence["_indices"][0]
        last_index = sentence["_indices"][-1]
        text = clean_text(sentence.get("text"))
        if not text:
            warnings.append(f"分段 {segment_id} 第 {index + 1} 句为空，未生成字幕")
            continue
        decision, reason = route(text, sentence.get("language", ""), languages[max(0, index - 2):index] + languages[last_index + 1:last_index + 3])
        for part, (start, stop, source) in enumerate(split_sentence(sentence)):
            start, stop = start + offset_ms, stop + offset_ms
            if stop <= 0:
                warnings.append(f"第 {index + 1} 句在播放器时间零点之前，保留原始结果并跳过负时间字幕")
                continue
            if start < 0:
                warnings.append(f"第 {index + 1} 句跨越时间零点，开始时间裁到 0")
                start = 0
            cue = {"cue_id": f"{segment_id}:{index:08}:{part:04}", "begin_ms": start, "end_ms": stop,
                   "text": source, "language": sentence.get("language", ""), "decision": decision, "reason": reason,
                   "asr_sentence_indices": sentence["_indices"]}
            if sentence["_adjustments"]:
                cue["timing_adjustments"] = sentence["_adjustments"]
            cues.append(cue)
    cues.sort(key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))
    for previous, current in zip(cues, cues[1:]):
        if current["begin_ms"] < previous["end_ms"]:
            warnings.append("识别时间存在重叠；保留两句真实时间，不截掉语音")
        if current["text"] == previous["text"]:
            warnings.append("相邻句文本相同；保留并提示复核，避免误删重复讲话")
    return cues, warnings


def mt_cache_key(text, source_lang="auto", region="beijing", *, account="", workspace_id="",
                 access_mode="legacy_public", processing_version=1):
    return hashlib.sha256(json.dumps({"model": MT_MODEL, "source": source_lang, "target": "Chinese",
                                     "text": text, "terms": [], "tm_list": [], "simplification": "opencc-t2s-0.1.7",
                                     "region": region, "account": account, "workspace_id": workspace_id,
                                     "access_mode": access_mode, "processing_version": processing_version,
                                     "cache_version": 2}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def timestamp(ms):
    if type(ms) is not int or ms < 0:
        raise ToolError("时间戳无效。", "timeline")
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def wrap(text, width):
    text = clean_text(text)
    # Preserve every character; prefer boundaries between words / after punctuation.
    lines = []
    while len(text) > width:
        cut = width
        space = text.rfind(" ", width // 2, width + 1)
        if space >= 0:
            cut = space
        else:
            for position in range(width, width // 2, -1):
                if text[position - 1] in "，。！？、,.;:!?":
                    cut = position
                    break
        lines.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        lines.append(text)
    return lines


def render_srt(cues, settings) -> bytes:
    rows, last = [], -1
    for number, cue in enumerate(cues, 1):
        if cue["begin_ms"] < last or cue["end_ms"] <= cue["begin_ms"]:
            raise ToolError("字幕时间轴无效或未排序。", "timeline")
        last = cue["begin_ms"]
        source = wrap(cue["text"], settings.zh_line_width if cue["language"] in {"zh", "yue"} else settings.other_line_width)
        if cue["decision"] == "translate":
            if not cue.get("translated"):
                raise ToolError("所需译文未全部完成，未写入残缺字幕。", "mt_incomplete")
            target = wrap(cue["translated"], settings.zh_line_width)
            source = target + source if settings.chinese_first else source + target
        elif cue["decision"] != "original":
            raise ToolError("仍有语种不确定项，需确认后生成字幕。", "language_uncertain")
        if not source or any("-->" in s for s in source):
            raise ToolError("字幕文本含异常时间箭头或为空。", "srt_invalid")
        rows.append(str(number) + "\r\n" + timestamp(cue["begin_ms"]) + " --> " + timestamp(cue["end_ms"]) + "\r\n" + "\r\n".join(source))
    return ("\r\n\r\n".join(rows) + ("\r\n\r\n" if rows else "")).encode("utf-8")


def write_srt(path: Path, content: bytes, overwrite=False):
    """Prepare beside target; back up before replace. Non-overwrite commit uses exclusive link."""
    if path.exists() and not overwrite:
        raise ToolError("同名 SRT 已存在，已跳过。", "exists")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temp, backup = Path(name), None
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            if path.exists():
                backup = path.with_name(path.name + f".bak.{time.time_ns()}")
                # Exclusive backup; any failure aborts before touching the original.
                with path.open("rb") as source, backup.open("xb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            os.replace(temp, path)
        else:
            # Atomic creation, including protection against a file appearing during cloud work.
            try:
                os.link(temp, path)
            except FileExistsError as e:
                raise ToolError("处理期间出现同名 SRT，已保留现有文件。", "exists") from e
            except OSError:
                if os.name == "nt":
                    # Windows rename fails when the destination exists, also on non-NTFS volumes.
                    os.rename(temp, path)
                else:
                    raise
    finally:
        temp.unlink(missing_ok=True)
    return str(backup) if backup else ""

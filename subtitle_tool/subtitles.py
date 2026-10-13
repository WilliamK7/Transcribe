from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path

from .common import MT_INPUT_BUDGET, MT_MODEL, MT_TEMPLATE_RESERVE, SUBTITLE_VERSION, ToolError

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


def normalize(raw: dict, offset_ms: int, segment_id: str, *, duration_ms=None,
              preserve_sentences=False, subtitle_version=SUBTITLE_VERSION) -> tuple[list[dict], list[str]]:
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
        parts = [(sentence["begin_time"], sentence["end_time"], text)] if preserve_sentences else split_sentence(sentence)
        for part, (start, stop, source) in enumerate(parts):
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
            if preserve_sentences:
                words, status = aligned_words(sentence, offset_ms, subtitle_version=subtitle_version)
                if start == 0 and sentence["begin_time"] + offset_ms < 0:
                    words, status = [], "clipped_timeline"
                cue.update(sentence_id=f"{segment_id}:{index:08}", words=words,
                           word_timing_status=status, char_start=0, char_end=len(text),
                           word_start=0, word_end=len(words), source_lang="auto", layout_round=0,
                           original_timings=[{"asr_sentence_index": i,
                                              "begin_ms": sentences[i]["begin_time"] + offset_ms,
                                              "end_ms": sentences[i]["end_time"] + offset_ms}
                                             for i in sentence["_indices"]])
                cue["cue_id"] = fragment_id(cue["sentence_id"], 0, len(words), 0, len(text))
            cues.append(cue)
    cues.sort(key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))
    for previous, current in zip(cues, cues[1:]):
        if current["begin_ms"] < previous["end_ms"]:
            warnings.append("识别时间存在重叠；保留两句真实时间，不截掉语音")
        if current["text"] == previous["text"]:
            warnings.append("相邻句文本相同；保留并提示复核，避免误删重复讲话")
    return cues, warnings


def mt_cache_key(text, source_lang="auto", region="beijing", *, account="", workspace_id="",
                 access_mode="legacy_public", processing_version=1, stage="legacy", strategy_version=0,
                 tm_list=(), asr_language=""):
    payload = {"model": MT_MODEL, "source": source_lang, "target": "Chinese",
               "text": text, "terms": [], "tm_list": [], "simplification": "opencc-t2s-0.1.7",
               "region": region, "account": account, "workspace_id": workspace_id,
               "access_mode": access_mode, "processing_version": processing_version, "cache_version": 2}
    if strategy_version:
        payload.update(cache_version=3, stage=stage, strategy_version=strategy_version, asr_language=asr_language,
                       context_sha256=hashlib.sha256(json.dumps(list(tm_list), ensure_ascii=False,
                                                               sort_keys=True).encode()).hexdigest())
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def timestamp(ms):
    if type(ms) is not int or ms < 0:
        raise ToolError("时间戳无效。", "timeline")
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def legacy_wrap(text, width):
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


OPENING = set("([{（［｛〈《「『【〔〖〘〚“‘«‹")
CLOSING = set(")] }）］｝〉》」』】〕〗〙〛”’»›，。！？、；：,.!?;:%％…".replace(" ", ""))


def display_width(text):
    return sum(0 if unicodedata.category(c).startswith("M") or c in "\u200d\ufe0f" else
               2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in text)


def _opening_at(text, index):
    c = text[index]
    return c in OPENING or (c in "\"'" and
                           (index == 0 or text[index - 1].isspace() or text[index - 1] in OPENING))


def _closing_at(text, index):
    return text[index] in CLOSING or (text[index] in "\"'" and not _opening_at(text, index))


def legal_break(text, position):
    left, right = position - 1, position
    while left >= 0 and text[left].isspace():
        left -= 1
    while right < len(text) and text[right].isspace():
        right += 1
    return (left >= 0 and right < len(text) and not _opening_at(text, left) and
            not _closing_at(text, right) and not unicodedata.category(text[right]).startswith("M") and
            text[right] != "\u200d" and text[left] != "\u200d")


def _wrap_positions(text):
    positions = [0]
    for i in range(1, len(text)):
        # Ordinary alphabetic tokens stay intact, including apostrophes and hyphens.
        if word_boundary(text, i) and legal_break(text, i) and not text[i - 1].isspace():
            positions.append(i)
    positions.append(len(text))
    return positions


def word_boundary(text, position):
    if not 0 < position < len(text):
        return False
    a, b = text[position - 1], text[position]
    return (a.isspace() or b.isspace() or unicodedata.east_asian_width(a) in {"W", "F"} or
            unicodedata.east_asian_width(b) in {"W", "F"})


def punctuation_overflow(line, width):
    at = len(line)
    while at and _closing_at(line, at - 1):
        at -= 1
    return display_width(line) <= width + 4 and display_width(line[:at]) <= width


def wrap(text, width):
    """Choose all breaks together: fewest lines, no avoidable short tail, balanced widths."""
    text = clean_text(text)
    if not text:
        return []
    if width <= 0:
        raise ValueError("Invalid display width")
    positions = _wrap_positions(text)
    # Each suffix records its best cost and next boundary. Candidate rows are capacity-bounded,
    # except for indivisible tokens and a small punctuation overhang.
    best = {len(positions) - 1: ((0, 0, 0), None)}
    total_words = len(LATIN.findall(text))
    for start in range(len(positions) - 2, -1, -1):
        choice = None
        for stop in range(start + 1, len(positions)):
            line = text[positions[start]:positions[stop]].strip()
            size = display_width(line)
            fits = size <= width or punctuation_overflow(line, width) or stop == start + 1
            if fits and stop in best:
                suffix = best[stop][0]
                short = int(stop == len(positions) - 1 and start > 0 and
                            ((total_words > 2 and 0 < len(LATIN.findall(line)) <= 2) or size < width / 4))
                cost = (suffix[0] + 1, suffix[1] + short, suffix[2] + (width - size) ** 2)
                if choice is None or cost < choice[0]:
                    choice = (cost, stop)
            if size > width + 4:
                break
        if choice:
            best[start] = choice
    lines, at = [], 0
    while at < len(positions) - 1:
        stop = best[at][1]
        lines.append(text[positions[at]:positions[stop]].strip())
        at = stop
    return lines


def aligned_words(sentence, offset_ms, *, subtitle_version=SUBTITLE_VERSION):
    """Map complete, monotonic ASR words onto the unchanged cleaned source text."""
    if sentence.get("_adjustments"):
        return [], "repaired_sentence"
    words = sentence.get("words")
    if sentence.get("language") not in WORD_LANGS or not isinstance(words, list) or not words:
        return [], "missing_words"
    text = clean_text(sentence["text"])
    positions = [i for i, c in enumerate(text) if not c.isspace()]
    compact = "".join(text[i] for i in positions)
    result, at, last = [], 0, sentence["begin_time"]
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not isinstance(word.get("punctuation", ""), str):
            return [], "invalid_words"
        b, e = word.get("begin_time"), word.get("end_time")
        if (type(b) is not int or type(e) is not int or b < last or e < b or
                (e == b and subtitle_version < 2) or e > sentence["end_time"]):
            return [], "invalid_word_timeline"
        value = re.sub(r"\s", "", clean_text(word["text"]) + word.get("punctuation", ""))
        if not value or compact[at:at + len(value)] != value:
            return [], "incomplete_word_text"
        result.append({"begin_ms": b + offset_ms, "end_ms": e + offset_ms,
                       "char_start": positions[at], "char_end": positions[at + len(value) - 1] + 1})
        at += len(value)
        last = e
    if at != len(compact):
        return [], "incomplete_word_text"
    if any(w["begin_ms"] == w["end_ms"] for w in result):
        groups, pending = [], []
        for word in result:
            if word["end_ms"] == word["begin_ms"]:
                pending.append(word)
                continue
            if pending:
                word = {**word, "begin_ms": pending[0]["begin_ms"], "char_start": pending[0]["char_start"]}
                pending = []
            groups.append(word)
        if not groups:
            return [], "invalid_word_timeline"
        if pending:
            groups[-1] = {**groups[-1], "end_ms": pending[-1]["end_ms"], "char_end": pending[-1]["char_end"]}
        return groups, "reliable_grouped_points"
    return result, "reliable"


def fragment_id(sentence_id, word_start, word_end, char_start, char_end):
    suffix = f"w{word_start:08}-{word_end:08}" if word_end else f"c{char_start:08}-{char_end:08}"
    return sentence_id + ":" + suffix


def make_fragment(source, lo=0, hi=None, *, layout_round=0):
    words = source.get("words", [])
    hi = len(words) if hi is None else hi
    full = lo == 0 and hi == len(words)
    char_start = 0 if full else words[lo]["char_start"]
    char_end = len(source["text"]) if full else words[hi - 1]["char_end"]
    # Whole sentences retain sentence timing. New pieces use only actual word endpoints.
    begin = source["begin_ms"] if full else words[lo]["begin_ms"]
    end = source["end_ms"] if full else words[hi - 1]["end_ms"]
    cue = {k: source[k] for k in ("sentence_id", "language", "decision", "reason")}
    cue.update(cue_id=fragment_id(source["sentence_id"], lo, hi, char_start, char_end),
               begin_ms=begin, end_ms=end, text=source["text"][char_start:char_end].strip(),
               source_lang=source.get("source_lang", "auto"), translated="", attempts=0, error="",
               char_start=char_start, char_end=char_end, word_start=lo, word_end=hi, layout_round=layout_round)
    for name in ["asr_sentence_indices", "timing_adjustments"]:
        if name in source:
            cue[name] = source[name]
    return cue


def _best_word_break(source, lo, hi):
    words, text = source.get("words", []), source["text"]
    choices = []
    for at in range(lo + 1, hi):
        if (words[at - 1]["end_ms"] - words[lo]["begin_ms"] < 1000 or
                words[hi - 1]["end_ms"] - words[at]["begin_ms"] < 1000):
            continue
        char = words[at - 1]["char_end"]
        if not legal_break(text, char) or not word_boundary(text, char):
            continue
        before = text[words[lo]["char_start"]:char].rstrip()
        # Sentence punctuation may be followed by closing quotes/brackets.
        stripped = before.rstrip("\"'”’）)]}」』】»›")
        sentence_end = bool(re.search(r"[。！？.!?]$", stripped))
        pause = words[at]["begin_ms"] - words[at - 1]["end_ms"] >= 400
        clause_end = bool(re.search(r"[，,；;：:]$", stripped))
        priority = 0 if sentence_end or pause else 1 if clause_end else 2
        after = text[words[at]["char_start"]:words[hi - 1]["char_end"]]
        balance = abs(display_width(before) - display_width(after))
        choices.append((priority, balance, at))
    return min(choices)[2] if choices else None


def source_capacity(source, settings):
    return settings.zh_line_width * 2 if source["language"] in {"zh", "yue"} else settings.other_line_width


def plan_fragments(source, settings, *, reference="", cue=None, layout_round=0, force=False):
    root = cue or make_fragment(source)
    words = source.get("words", [])
    if len(words) < 2 or root["end_ms"] - root["begin_ms"] < 1000:
        return [root]
    root_width = max(1, display_width(root["text"]))
    target_estimate = display_width(reference)
    # A final translation is used only to estimate capacity, never to assign translated substrings.
    stack = [(root["word_start"], root["word_end"], True)]
    ranges = []
    while stack:
        lo, hi, first = stack.pop()
        part = make_fragment(source, lo, hi, layout_round=layout_round)
        estimate = target_estimate * display_width(part["text"]) / root_width
        over = (len(wrap(part["text"], source_capacity(source, settings))) > 2 or
                estimate > settings.zh_line_width * 4 or
                (first and reference and len(wrap(reference, settings.zh_line_width * 2)) > 2))
        needs = over or part["end_ms"] - part["begin_ms"] > 7000 or (first and force)
        at = _best_word_break(source, lo, hi) if needs else None
        if at is None:
            ranges.append((lo, hi))
        else:
            stack.extend([(at, hi, False), (lo, at, False)])
    if ranges == [(root["word_start"], root["word_end"])]:
        return [root]
    return [make_fragment(source, lo, hi, layout_round=layout_round) for lo, hi in sorted(ranges)]


def mt_input_size(text, source_lang="auto", tm_list=()):
    payload = {"messages": [{"role": "user", "content": text}],
               "translation_options": {"source_lang": source_lang, "target_lang": "Chinese"}}
    if tm_list:
        payload["translation_options"]["tm_list"] = list(tm_list)
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + MT_TEMPLATE_RESERVE


def validate_mt_input(text, source_lang="auto", tm_list=()):
    if mt_input_size(text, source_lang, tm_list) > MT_INPUT_BUDGET:
        raise ToolError("待翻译文本超过保守输入预算，且无法在可靠词时间边界安全拆分；进度已保存，可恢复。", "mt_input_budget")


def plan_reference_units(sources):
    units = []
    for source in sources:
        if source["decision"] != "translate":
            continue
        root = make_fragment(source)
        stack = [root]
        while stack:
            cue = stack.pop()
            if mt_input_size(cue["text"], cue["source_lang"]) > MT_INPUT_BUDGET:
                at = _best_word_break(source, cue["word_start"], cue["word_end"])
                if at is None:
                    validate_mt_input(cue["text"], cue["source_lang"])
                stack.extend([make_fragment(source, at, cue["word_end"]),
                              make_fragment(source, cue["word_start"], at)])
                continue
            cue["cue_id"] += ":reference"
            units.append(cue)
    return sorted(units, key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))


def reference_sources(sources, references):
    grouped = {}
    by_id = {s["sentence_id"]: s for s in sources}
    for reference in references:
        source = by_id.get(reference.get("sentence_id"))
        if source is None:
            raise ToolError("参考翻译的完整句来源记录不一致。", "cache_mismatch")
        validate_fragment(source, reference, reference_stage=True)
        if not reference.get("translated"):
            raise ToolError("整句参考译文未全部完成，已保存进度。", "mt_incomplete")
        grouped.setdefault(reference["sentence_id"], []).append(reference)
    for source in sources:
        if source["decision"] != "translate":
            continue
        units = sorted(grouped.get(source["sentence_id"], []), key=lambda c: c["char_start"])
        at = 0
        for unit in units:
            if unit["char_start"] < at or source["text"][at:unit["char_start"]].strip():
                raise ToolError("参考翻译的文字范围不连续。", "cache_mismatch")
            at = unit["char_end"]
        if not units or source["text"][at:].strip():
            raise ToolError("整句参考翻译记录不完整，不能跳过必要阶段。", "cache_mismatch")
    return [{**source, "reference": clean_text(" ".join(c["translated"] for c in
                            sorted(grouped.get(source["sentence_id"], []), key=lambda c: c["char_start"])))}
            for source in sources]


def validate_fragment(source, cue, *, reference_stage=False):
    lo, hi = cue.get("word_start"), cue.get("word_end")
    count = len(source.get("words", []))
    if (type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi <= count or
            (count and lo == hi)):
        raise ToolError("字幕片段的词范围无效。", "cache_mismatch")
    expected = make_fragment(source, cue["word_start"], cue["word_end"])
    if reference_stage:
        expected["cue_id"] += ":reference"
    if any(cue[k] != expected[k] for k in ["cue_id", "sentence_id", "begin_ms", "end_ms", "text", "language",
                                         "decision", "source_lang", "word_start", "word_end", "char_start", "char_end"]):
        raise ToolError("字幕片段与完整句来源／词范围记录不一致。", "cache_mismatch")


def translation_context(cue, sources):
    position = next(i for i, s in enumerate(sources) if s["sentence_id"] == cue["sentence_id"])
    source = sources[position]
    candidates = []
    if cue["char_start"] != 0 or cue["char_end"] != len(source["text"]):
        candidates.append(source)
    def compatible(s):
        language = s["language"].lower().strip()
        manual = s["reason"] == "用户在终端确认"
        if not language:
            # An absent label with automatic MT detection is not evidence of matching languages.
            language = s.get("source_lang") if manual and s.get("source_lang") != "auto" else s["sentence_id"]
        return language, s["decision"], s.get("source_lang", "auto"), manual
    neighbors = []
    for direction in [-1, 1]:
        previous = source
        for distance in [1, 2]:
            index = position + direction * distance
            if not 0 <= index < len(sources):
                break
            neighbor = sources[index]
            gap = (previous["begin_ms"] - neighbor["end_ms"] if direction < 0 else
                   neighbor["begin_ms"] - previous["end_ms"])
            if gap > 5000 or compatible(neighbor) != compatible(source):
                break
            proximity = max(neighbor["begin_ms"] - cue["end_ms"], cue["begin_ms"] - neighbor["end_ms"], 0)
            neighbors.append((proximity, distance, index, neighbor))
            previous = neighbor
    candidates.extend(item[3] for item in sorted(neighbors, key=lambda n: n[:3]))
    context = []
    for candidate in candidates:
        if not candidate.get("reference"):
            continue
        item = {"source": candidate["text"], "target": candidate["reference"]}
        if mt_input_size(cue["text"], cue["source_lang"], context + [item]) <= MT_INPUT_BUDGET:
            context.append(item)
    validate_mt_input(cue["text"], cue["source_lang"], context)
    return context


def layout_warnings(cues, settings):
    warnings = []
    for cue in cues:
        for name, text, width in [("原文", cue["text"], source_capacity(cue, settings)),
                                  ("译文", cue.get("translated", ""), settings.zh_line_width * 2)]:
            lines = wrap(text, width)
            over_width = any(display_width(line) > width and not punctuation_overflow(line, width) for line in lines)
            if len(lines) > 2 or over_width:
                warnings.append(f"片段 {cue['cue_id']} {name}排版超限（{len(lines)} 行）；可靠词时间／最短时长／不可拆词或追加轮数限制，保留完整内容与已有时间")
    return warnings


def render_srt(cues, settings, *, subtitle_version=SUBTITLE_VERSION) -> bytes:
    rows, last = [], -1
    for number, cue in enumerate(cues, 1):
        if cue["begin_ms"] < last or cue["end_ms"] <= cue["begin_ms"]:
            raise ToolError("字幕时间轴无效或未排序。", "timeline")
        last = cue["begin_ms"]
        wrapping = wrap if subtitle_version else legacy_wrap
        zh_width = settings.zh_line_width * (2 if subtitle_version else 1)
        source = wrapping(cue["text"], zh_width if cue["language"] in {"zh", "yue"} else settings.other_line_width)
        if cue["decision"] == "translate":
            if not cue.get("translated"):
                raise ToolError("所需译文未全部完成，未写入残缺字幕。", "mt_incomplete")
            target = wrapping(cue["translated"], zh_width)
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

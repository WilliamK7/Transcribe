from __future__ import annotations

import re
import unicodedata

from .common import DISPLAY_TIMING_VERSION, SUPPORTED_DISPLAY_TIMING_VERSIONS, ToolError
from .review import decision_origin

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


def _corroborated_point(sentence, text):
    words = sentence.get("words")
    anchor = sentence["begin_time"]
    if (not text or sentence.get("language") not in WORD_LANGS or
            not isinstance(words, list) or not words or any(
                not isinstance(word, dict) or type(word.get("begin_time")) is not int or
                type(word.get("end_time")) is not int or word["begin_time"] != anchor or
                word["end_time"] != anchor or not isinstance(word.get("text"), str) or
                not isinstance(word.get("punctuation", ""), str) for word in words)):
        return False
    return (re.sub(r"\s", "", "".join(w["text"] + w.get("punctuation", "") for w in words)) ==
            re.sub(r"\s", "", text))


def _merge_boundary_point_groups(records, segment_id):
    """Resolve long point groups before any short point receives display timing."""
    warnings, at = [], 0
    original_intervals = {sentence["_indices"][0]: (sentence["begin_time"], sentence["end_time"])
                          for sentence in records if sentence["begin_time"] < sentence["end_time"]}
    while at < len(records):
        anchor = records[at]["begin_time"]
        if records[at]["end_time"] != anchor:
            at += 1
            continue
        stop = at + 1
        while (stop < len(records) and
               records[stop]["begin_time"] == records[stop]["end_time"] == anchor):
            stop += 1
        group = records[at:stop]
        if not any(len(clean_text(sentence.get("text"))) > 12 for sentence in group):
            at = stop
            continue
        language = group[0].get("language")
        for sentence in group:
            if sentence.get("language") != language or not _corroborated_point(sentence, clean_text(sentence.get("text"))):
                index = sentence["_indices"][0]
                raise ToolError(f"第 {index + 1} 句同点组缺少一致的句词证据；原始结果已保留。", "timeline")
        previous = records[at - 1] if at else None
        following = records[stop] if stop < len(records) else None
        ordered = ((previous is None or previous["end_time"] <= anchor) and
                   (following is None or following["begin_time"] >= anchor))
        candidates = []
        if ordered:
            for candidate, side in [(previous, "previous"), (following, "next")]:
                if (candidate and candidate.get("language") == language and clean_text(candidate.get("text")) and
                        candidate["begin_time"] < candidate["end_time"]):
                    interval = next((original_intervals[i] for i in candidate["_indices"]
                                     if i in original_intervals), None)
                    if interval is not None:
                        distance = max(interval[0] - anchor, anchor - interval[1], 0)
                        candidates.append((candidate, side, interval, distance))
        neighbor, direction, original_interval, distance = None, None, None, 0
        # Try both exact boundaries before the existing short-point 400ms fallback.
        # Always measure from raw positive timing, never an endpoint widened by an earlier group.
        for limit in (0, 400):
            for candidate in candidates:
                if candidate[3] <= limit:
                    neighbor, direction, original_interval, distance = candidate
                    break
            if neighbor is not None:
                break
        if neighbor is None:
            index = group[0]["_indices"][0]
            raise ToolError(f"第 {index + 1} 句同点组没有距离 400ms 内的同语种原始正时长邻句；原始结果已保留。", "timeline")
        for sentence in group:
            adjustment = {
                "asr_sentence_index": sentence["_indices"][0], "anchor_ms": anchor,
                "original_begin_ms": anchor, "original_end_ms": anchor,
                "kind": "merged_asr_boundary_point", "direction": direction}
            if distance:
                adjustment.update(kind="merged_asr_nearby_point", distance_ms=distance,
                                  original_neighbor_begin_ms=original_interval[0],
                                  original_neighbor_end_ms=original_interval[1])
            sentence["_adjustments"].append(adjustment)
        ordered_group = [neighbor, *group] if direction == "previous" else [*group, neighbor]
        joiner = "" if language in {"zh", "yue", "ja"} else " "
        merged = {**neighbor, "begin_time": min(anchor, neighbor["begin_time"]),
                  "end_time": max(anchor, neighbor["end_time"]),
                  "text": joiner.join(clean_text(sentence["text"]) for sentence in ordered_group),
                  "words": None, "_indices": [i for sentence in ordered_group for i in sentence["_indices"]],
                  "_adjustments": [a for sentence in ordered_group for a in sentence["_adjustments"]]}
        if direction == "previous":
            records[at - 1:stop] = [merged]
        else:
            records[at:stop + 1] = [merged]
        detail = (f"合并距原始正时长区间 {distance}ms 的同语种邻句；显示区间覆盖邻句和 ASR 点，"
                  "保留全部文字和原始句级时间，未构造词级时间" if distance else
                  "合并边界相接的同语种原始正时长邻句；保留全部文字和已有句级边界，未构造词级时间")
        warnings.append(f"分段 {segment_id} 第 {group[0]['_indices'][0] + 1}～{group[-1]['_indices'][-1] + 1} 句零时长，" + detail)
        if direction == "next":
            at += 1
    return records, warnings


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
    records, warnings = _merge_boundary_point_groups(records, segment_id)
    at = 0
    while at < len(records):
        sentence = records[at]
        b, e = sentence["begin_time"], sentence["end_time"]
        if e > b:
            at += 1
            continue
        index = sentence["_indices"][0]
        text = clean_text(sentence.get("text"))
        if len(text) > 12 or not _corroborated_point(sentence, text):
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
        if "text" in transcript and clean_text(transcript["text"]):
            if any(not isinstance(s, dict) for s in transcript["sentences"]):
                raise ToolError("识别句子结构无效。", "response_schema")
            if not any(clean_text(s.get("text")) for s in transcript["sentences"]):
                raise ToolError("识别汇总含文字但没有有效句子；原始结果已保留。", "response_schema")
        sentences.extend(transcript["sentences"])
    if duration_ms is not None:
        if type(duration_ms) is not int or duration_ms <= 0:
            raise ToolError("分段时长无效，不能验证识别时间范围。", "timeline")
        for index, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                raise ToolError("识别句子结构无效。", "response_schema")
            b, e = sentence.get("begin_time"), sentence.get("end_time")
            if clean_text(sentence.get("text")) and (type(b) is not int or type(e) is not int or
                                                      b >= duration_ms or e > duration_ms + 1):
                raise ToolError(f"第 {index + 1} 句超出本段时间范围；原始结果已保留。", "timeline")
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
        start, stop = sentence["begin_time"] + offset_ms, sentence["end_time"] + offset_ms
        if stop <= 0:
            warnings.append(f"第 {index + 1} 句在播放器时间零点之前，保留原始结果并跳过负时间字幕")
            continue
        if start < 0:
            warnings.append(f"第 {index + 1} 句跨越时间零点，开始时间裁到 0")
        words, status = aligned_words(sentence, offset_ms)
        if start < 0:
            words, status = [], "clipped_timeline"
        sentence_id = f"{segment_id}:{index:08}"
        cue = {"cue_id": fragment_id(sentence_id, 0, len(words), 0, len(text)),
               "sentence_id": sentence_id, "begin_ms": max(0, start), "end_ms": stop,
               "text": text, "language": sentence.get("language", ""), "decision": decision, "reason": reason,
               "decision_origin": "automatic", "asr_sentence_indices": sentence["_indices"],
               "words": words, "word_timing_status": status, "char_start": 0, "char_end": len(text),
               "word_start": 0, "word_end": len(words), "source_lang": "auto", "layout_round": 0,
               "original_timings": [{"asr_sentence_index": i,
                                     "begin_ms": sentences[i]["begin_time"] + offset_ms,
                                     "end_ms": sentences[i]["end_time"] + offset_ms}
                                    for i in sentence["_indices"]]}
        if sentence["_adjustments"]:
            cue["timing_adjustments"] = sentence["_adjustments"]
        cues.append(cue)
    cues.sort(key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))
    for previous, current in zip(cues, cues[1:]):
        if current["begin_ms"] < previous["end_ms"]:
            warnings.append("识别时间存在重叠；保留原始时间依据，最终显示区间按任务策略处理")
        if current["text"] == previous["text"]:
            warnings.append("相邻句文本相同；保留并提示复核，避免误删重复讲话")
    return cues, warnings


def timestamp(ms):
    if type(ms) is not int or ms < 0:
        raise ToolError("时间戳无效。", "timeline")
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


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
    if display_width(text) <= width or punctuation_overflow(text, width):
        return [text]
    positions = _wrap_positions(text)
    widths = [0]
    for char in text:
        widths.append(widths[-1] + display_width(char))
    # clean_text collapses whitespace; each boundary is after a nonspace.
    # A row can therefore have at most one leading space and no trailing one.
    starts = [position + int(position < len(text) and text[position] == ' ') for position in positions]
    # Each suffix records its best cost and next boundary. Candidate rows are capacity-bounded,
    # except for indivisible tokens and a small punctuation overhang.
    best = {len(positions) - 1: ((0, 0, 0), None)}
    total_words = len(LATIN.findall(text))
    for start in range(len(positions) - 2, -1, -1):
        choice = None
        for stop in range(start + 1, len(positions)):
            begin, end = starts[start], positions[stop]
            size = widths[end] - widths[begin]
            fits = (size <= width or stop == start + 1 or
                    size <= width + 4 and punctuation_overflow(text[begin:end], width))
            if fits and stop in best:
                suffix = best[stop][0]
                short = int(stop == len(positions) - 1 and start > 0 and
                            ((total_words > 2 and 0 < len(LATIN.findall(text[begin:end])) <= 2) or size < width / 4))
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


def aligned_words(sentence, offset_ms):
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
                e > sentence["end_time"]):
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
    cue["decision_origin"] = decision_origin(source)
    cue.update(cue_id=fragment_id(source["sentence_id"], lo, hi, char_start, char_end),
               begin_ms=begin, end_ms=end, text=source["text"][char_start:char_end].strip(),
               source_lang=source.get("source_lang", "auto"), translated="",
               char_start=char_start, char_end=char_end, word_start=lo, word_end=hi, layout_round=layout_round)
    for name in ["asr_sentence_indices", "timing_adjustments"]:
        if name in source:
            cue[name] = source[name]
    return cue


class _WordBreakIndex:
    """Index one source in O(characters + words); each range search scans its words."""
    def __init__(self, source):
        text, words = source["text"], source.get("words", [])
        widths = [0]
        for char in text:
            width = (0 if unicodedata.category(char).startswith("M") or char in "\u200d\ufe0f" else
                     2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
            widths.append(widths[-1] + width)
        candidates = [None] * len(words)
        for at in range(1, len(words)):
            char = words[at - 1]["char_end"]
            if not word_boundary(text, char) or not legal_break(text, char):
                continue
            before_end = char
            while before_end and text[before_end - 1].isspace():
                before_end -= 1
            punctuation_end = before_end
            while punctuation_end and text[punctuation_end - 1] in "\"'”’）)]}」』】»›":
                punctuation_end -= 1
            # A newline before closing quotes still leaves the preceding punctuation significant.
            if punctuation_end and text[punctuation_end - 1] == "\n":
                punctuation_end -= 1
            tail = text[punctuation_end - 1] if punctuation_end else ""
            priority = 0 if tail and tail in "。！？.!?" else 1 if tail and tail in "，,；;：:" else 2
            pause = words[at]["begin_ms"] - words[at - 1]["end_ms"] >= 400
            candidates[at] = (widths[before_end], punctuation_end, priority, pause,
                              widths[words[at]["char_start"]])
        self._words, self._widths, self._candidates = words, widths, candidates

    def best(self, lo, hi):
        if hi - lo < 2:
            return None
        words, widths = self._words, self._widths
        start = words[lo]["char_start"]
        start_width, end_width = widths[start], widths[words[hi - 1]["char_end"]]
        begin_ms, end_ms = words[lo]["begin_ms"], words[hi - 1]["end_ms"]
        choice = None
        for at in range(lo + 1, hi):
            candidate = self._candidates[at]
            if (candidate is None or words[at - 1]["end_ms"] - begin_ms < 1000 or
                    end_ms - words[at]["begin_ms"] < 1000):
                continue
            before_width, punctuation_end, punctuation_priority, pause, after_start_width = candidate
            priority = 0 if pause else punctuation_priority if punctuation_end > start else 2
            # rstrip and quote removal stop at this range's left edge; the right slice is untrimmed.
            balance = abs(max(0, before_width - start_width) - (end_width - after_start_width))
            score = (priority, balance, at)
            if choice is None or score < choice:
                choice = score
        return choice[2] if choice else None


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
    break_index = None
    while stack:
        lo, hi, first = stack.pop()
        part = make_fragment(source, lo, hi, layout_round=layout_round)
        estimate = target_estimate * display_width(part["text"]) / root_width
        over = (len(wrap(part["text"], source_capacity(source, settings))) > 2 or
                estimate > settings.zh_line_width * 4 or
                (first and reference and len(wrap(reference, settings.zh_line_width * 2)) > 2))
        needs = over or part["end_ms"] - part["begin_ms"] > 7000 or (first and force)
        at = None
        if needs:
            if break_index is None:
                break_index = _WordBreakIndex(source)
            at = break_index.best(lo, hi)
        if at is None:
            ranges.append((lo, hi))
        else:
            stack.extend([(at, hi, False), (lo, at, False)])
    if ranges == [(root["word_start"], root["word_end"])]:
        return [root]
    return [make_fragment(source, lo, hi, layout_round=layout_round) for lo, hi in sorted(ranges)]


def split_mt_units(source, units, *, fits):
    result, break_index = [], None
    stack = list(reversed(units))
    while stack:
        unit = stack.pop()
        if not fits(unit):
            if break_index is None:
                break_index = _WordBreakIndex(source)
            at = break_index.best(unit['word_start'], unit['word_end'])
            if at is None:
                raise ToolError('完整请求超过模型预算且没有可靠词边界。', 'mt_input_budget')
            stack.extend([make_fragment(source, at, unit['word_end'], layout_round=unit.get('layout_round', 0)),
                          make_fragment(source, unit['word_start'], at, layout_round=unit.get('layout_round', 0))])
            continue
        result.append(unit)
    return result


def source_context(sources, position, last_position=None):
    """Neighbors outside the whole target range, ordered for budget retention."""
    sentence_limit, gap_limit = 12, 15000
    last_position = position if last_position is None else last_position
    def compatible(item):
        language = item['language'].lower().strip()
        manual = decision_origin(item) == 'manual'
        if not language:
            language = item.get('source_lang') if manual and item.get('source_lang') != 'auto' else item['sentence_id']
        return language, item['decision'], item.get('source_lang', 'auto'), manual
    neighbors = []
    for direction in [-1, 1]:
        edge = position if direction < 0 else last_position
        source = sources[edge]
        boundary = compatible(source)
        previous = source
        for distance in range(1, sentence_limit + 1):
            index = edge + direction * distance
            if not 0 <= index < len(sources):
                break
            neighbor = sources[index]
            gap = (previous['begin_ms'] - neighbor['end_ms'] if direction < 0 else
                   neighbor['begin_ms'] - previous['end_ms'])
            proximity = max(neighbor['begin_ms'] - source['end_ms'], source['begin_ms'] - neighbor['end_ms'], 0)
            if (gap > gap_limit or compatible(neighbor) != boundary or
                    proximity > 30000):
                break
            neighbors.append((proximity, distance, index, neighbor))
            previous = neighbor
    return [{'position': 'before' if index < position else 'after', 'text': neighbor['text'],
             'sentence_id': neighbor['sentence_id'], 'source_position': index}
            for _, _, index, neighbor in sorted(neighbors, key=lambda n: n[:3])]


def validate_unit_coverage(source, units):
    at, word = 0, 0
    for unit in units:
        validate_fragment(source, unit)
        if (unit['char_start'] < at or source['text'][at:unit['char_start']].strip() or unit['word_start'] != word):
            raise ToolError("同句片段的原文／词范围不连续。", "cache_mismatch")
        at, word = unit['char_end'], unit['word_end']
    if not units or source['text'][at:].strip() or word != len(source.get('words', [])):
        raise ToolError("同句片段未覆盖完整来源，不能输出不完整字幕。", "cache_mismatch")


def validate_fragment(source, cue):
    if decision_origin(source) != decision_origin(cue):
        raise ToolError("字幕片段与完整句的人工决定来源不一致。", "cache_mismatch")
    lo, hi = cue.get("word_start"), cue.get("word_end")
    count = len(source.get("words", []))
    if (type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi <= count or
            (count and lo == hi)):
        raise ToolError("字幕片段的词范围无效。", "cache_mismatch")
    expected = make_fragment(source, cue["word_start"], cue["word_end"])
    if any(cue[k] != expected[k] for k in ["cue_id", "sentence_id", "begin_ms", "end_ms", "text", "language",
                                         "decision", "source_lang", "word_start", "word_end", "char_start", "char_end"]):
        raise ToolError("字幕片段与完整句来源／词范围记录不一致。", "cache_mismatch")


def revise_layout(cues, sources, settings, *, layout_round):
    """Split overflowing translations; joint groups must be translated together again."""
    revised, affected = [], set()
    for cue in cues:
        parts = [cue]
        if cue['decision'] == 'translate' and len(wrap(cue['translated'], settings.zh_line_width * 2)) > 2:
            parts = plan_fragments(sources[cue['sentence_id']], settings, cue=cue,
                                   reference=cue['translated'], layout_round=layout_round, force=True)
        if len(parts) > 1:
            affected.add(cue['sentence_id'])
        revised.extend(parts)
    if not affected:
        return None
    revised = [{**cue, 'translated': ''} if cue['sentence_id'] in affected else cue
               for cue in revised]
    return revised


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


def _timeline_error(*cues, detail=None):
    ranges = " 与 ".join(f"{c.get('sentence_id', c['cue_id'])} / {c['cue_id']} "
                        f"[{c.get('begin_ms')}, {c.get('end_ms')})ms" for c in cues)
    explanation = ('；' + detail) if detail else ''
    return ToolError("字幕显示时间冲突：" + ranges + explanation + "；结果已保留，需人工确认。", "timeline")


def validate_display_intervals(cues, intervals):
    """Validate the whole display timeline, independently of the ASR evidence."""
    if len(intervals) != len(cues):
        raise ToolError("字幕显示区间数量与片段不一致。", "timeline")
    previous, identities = None, set()
    for cue, interval in zip(cues, intervals):
        begin, end = interval.get("begin_ms"), interval.get("end_ms")
        if (interval.get("cue_id") != cue["cue_id"] or cue["cue_id"] in identities or
                type(begin) is not int or type(end) is not int or not 0 <= begin < end):
            raise _timeline_error(cue)
        if previous is not None and begin < previous["end_ms"]:
            raise _timeline_error(previous, {**interval, "sentence_id": cue.get("sentence_id", cue["cue_id"])})
        identities.add(cue["cue_id"])
        previous = {**interval, "sentence_id": cue.get("sentence_id", cue["cue_id"])}


def _display_trim_issue(original, remaining):
    duration = original['end_ms'] - original['begin_ms']
    trimmed = duration - remaining
    if trimmed > 500:
        return f'累计缩短超限：{trimmed}ms，最多允许 500ms'
    if trimmed * 10 > duration:
        return f'缩短比例超限：{trimmed}ms / {duration}ms，最多允许原显示时长的 10%'
    if remaining < 1000:
        return f'时长不足：修整后 {remaining}ms，须至少 1000ms'
    return None


def _trim_display_interval(interval, begin, end, reason):
    old_begin, old_end = interval['begin_ms'], interval['end_ms']
    interval.update(begin_ms=begin, end_ms=end)
    return {'cue_id': interval['cue_id'], 'old_begin_ms': old_begin, 'old_end_ms': old_end,
            'new_begin_ms': begin, 'new_end_ms': end,
            'adjustment_ms': (begin - old_begin) + (old_end - end), 'reason': reason}


def plan_display_timing(cues, *, version=DISPLAY_TIMING_VERSION):
    """Derive bounded display adjustments without altering recognition evidence."""
    if type(version) is not int or version not in SUPPORTED_DISPLAY_TIMING_VERSIONS:
        raise ToolError('任务显示时间版本不受支持，请用 --reflow 显式重排。', 'cache_version')
    intervals, adjustments = [], []
    for index, cue in enumerate(cues):
        begin, end = cue["begin_ms"], cue["end_ms"]
        if type(begin) is not int or type(end) is not int or not 0 <= begin < end:
            raise _timeline_error(cue, detail='区间结构冲突：时间须为非负整数且起点小于终点')
        current = {"cue_id": cue["cue_id"], "begin_ms": begin, "end_ms": end}
        if intervals:
            previous = intervals[-1]
            prior_cue = cues[index - 1]
            if begin < previous["end_ms"]:
                source_ids = [c.get("sentence_id") for c in (prior_cue, cue)]
                parts = [re.fullmatch(r"([A-Za-z0-9_-]{1,80}):(\d{8})", sid) if isinstance(sid, str) else None
                         for sid in source_ids]
                overlap = previous["end_ms"] - begin
                structure_ok = (all(parts) and parts[0][1] == parts[1][1] and int(parts[0][2]) < int(parts[1][2]) and
                                previous['begin_ms'] < begin < previous['end_ms'] < end)
                if not structure_ok:
                    detail = '区间结构冲突：须同分段、来源递增且前起点 < 后起点 < 前终点 < 后终点'
                    raise _timeline_error(prior_cue, cue, detail=detail)
                if overlap > 500:
                    raise _timeline_error(prior_cue, cue, detail=f'重叠超限：{overlap}ms，最多允许 500ms')
                previous_issue = _display_trim_issue(prior_cue, begin - previous['begin_ms'])
                if version == 2 and previous_issue is None and end - begin < 1000:
                    previous_issue = f'时长不足：后条原时长 {end - begin}ms，须至少 1000ms'
                if previous_issue is None:
                    adjustments.append(_trim_display_interval(previous, previous['begin_ms'], begin,
                                                              'bounded_sentence_overlap'))
                elif version == 2:
                    raise _timeline_error(prior_cue, cue, detail=previous_issue)
                else:
                    next_issue = _display_trim_issue(cue, end - previous['end_ms'])
                    if next_issue:
                        raise _timeline_error(prior_cue, cue, detail=f'前条{previous_issue}；后条{next_issue}')
                    adjustments.append(_trim_display_interval(current, previous['end_ms'], end,
                                                              'bounded_sentence_overlap_start_delay'))
        intervals.append(current)
    validate_display_intervals(cues, intervals)
    return intervals, adjustments


def render_srt(cues, settings, *, display_intervals=None) -> bytes:
    if display_intervals is not None:
        validate_display_intervals(cues, display_intervals)
    rows, last = [], -1
    for number, cue in enumerate(cues, 1):
        if cue["begin_ms"] < last or cue["end_ms"] <= cue["begin_ms"]:
            raise ToolError("字幕时间轴无效或未排序。", "timeline")
        last = cue["begin_ms"]
        zh_width = settings.zh_line_width * 2
        source = wrap(cue["text"], zh_width if cue["language"] in {"zh", "yue"} else settings.other_line_width)
        if cue["decision"] == "translate":
            if not cue.get("translated"):
                raise ToolError("所需译文未全部完成，未写入残缺字幕。", "mt_incomplete")
            target = wrap(cue["translated"], zh_width)
            source = target + source if settings.chinese_first else source + target
        elif cue["decision"] != "original":
            raise ToolError("仍有语种不确定项，需确认后生成字幕。", "language_uncertain")
        if not source or any("-->" in s for s in source):
            raise ToolError("字幕文本含异常时间箭头或为空。", "srt_invalid")
        timing = cue if display_intervals is None else display_intervals[number - 1]
        rows.append(str(number) + "\r\n" + timestamp(timing["begin_ms"]) + " --> " + timestamp(timing["end_ms"]) + "\r\n" + "\r\n".join(source))
    return ("\r\n\r\n".join(rows) + ("\r\n\r\n" if rows else "")).encode("utf-8")

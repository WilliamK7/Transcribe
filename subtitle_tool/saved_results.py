from __future__ import annotations

import json
import re

from .common import ToolError
from .review import decision_origin
from .subtitles import clean_text, normalize, validate_fragment, validate_unit_coverage


def mismatch():
    return ToolError("已保存的字幕／转写记录不完整或互相矛盾；原任务已保留，不会重新识别。", "cache_mismatch")


def segment_offset(manifest):
    plan = manifest.get("media_plan") or {}
    offset = plan.get("audio_offset_ms", manifest.get("audio_offset_ms"))
    if (type(offset) is not int or
            "audio_offset_ms" in manifest and manifest["audio_offset_ms"] != offset):
        raise mismatch()
    return offset


def validate_segments(manifest):
    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        raise mismatch()
    segment_offset(manifest)
    identities, previous = set(), -1
    for segment in segments:
        if not isinstance(segment, dict):
            raise mismatch()
        identity, offset = segment.get("id"), segment.get("offset_ms")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity) or
                identity in identities or type(offset) is not int or offset < 0 or offset <= previous or
                "duration_ms" in segment and (type(segment["duration_ms"]) is not int or segment["duration_ms"] <= 0) or
                segment.get("raw", "raw-" + identity + ".json") != "raw-" + identity + ".json"):
            raise mismatch()
        if segment.get("task_id") and (not isinstance(segment["task_id"], str) or
                                       not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", segment["task_id"])):
            raise mismatch()
        identities.add(identity)
        previous = offset


def raw_path(cache, segment):
    return cache / ("raw-" + segment["id"] + ".json")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise mismatch() from error


def normalize_raw(cache, manifest, segment):
    return normalize(read_json(raw_path(cache, segment)), segment_offset(manifest) + segment["offset_ms"], segment["id"],
                     duration_ms=segment.get("duration_ms"))


def _validate_source(source, segments, manifest):
    identity = source["sentence_id"]
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}:\d{8}", identity):
        raise mismatch()
    segment = segments.get(identity.split(":")[0])
    if segment is None:
        raise mismatch()
    _validate_record(source)
    words = source["words"]
    if not isinstance(words, list):
        raise mismatch()
    char, end = 0, source["begin_ms"]
    for word in words:
        b, e, lo, hi = (word[k] for k in ["begin_ms", "end_ms", "char_start", "char_end"])
        if (any(type(v) is not int for v in [b, e, lo, hi]) or not end <= b < e <= source["end_ms"] or
                not char <= lo < hi <= len(source["text"]) or source["text"][char:lo].strip()):
            raise mismatch()
        char, end = hi, e
    if words and source["text"][char:].strip():
        raise mismatch()
    if source["word_start"] != 0 or source["word_end"] != len(words):
        raise mismatch()
    validate_fragment(source, source)
    if "duration_ms" in segment:
        start = segment_offset(manifest) + segment["offset_ms"]
        if source["begin_ms"] < max(0, start) or source["end_ms"] > start + segment["duration_ms"] + 1:
            raise mismatch()


def _validate_record(record):
    decision_origin(record)
    if (type(record["begin_ms"]) is not int or type(record["end_ms"]) is not int or
            not 0 <= record["begin_ms"] < record["end_ms"] or
            not isinstance(record["text"], str) or not record["text"] or clean_text(record["text"]) != record["text"] or
            not isinstance(record["language"], str) or record["decision"] not in {"original", "translate", "uncertain"}):
        raise mismatch()


def validate_normalized(manifest, sources, cues):
    """Prove source and fragment coverage; MT provenance is checked by the pipeline."""
    try:
        validate_segments(manifest)
        count = manifest.get('normalized_count')
        if type(count) is not int or count != len(sources):
            raise mismatch()
        by_id = {s["sentence_id"]: s for s in sources}
        if len(by_id) != len(sources):
            raise mismatch()
        segments = {s["id"]: s for s in manifest["segments"]}
        grouped = {}
        for source in sources:
            _validate_source(source, segments, manifest)
        if len({c['cue_id'] for c in cues}) != len(cues):
            raise mismatch()
        for cue in cues:
            source = by_id.get(cue.get('sentence_id'))
            if source is None:
                raise mismatch()
            validate_fragment(source, cue)
            grouped.setdefault(source['sentence_id'], []).append(cue)
        for source in sources:
            validate_unit_coverage(source, sorted(grouped.get(source['sentence_id'], []), key=lambda c: c['char_start']))
    except (KeyError, ValueError, TypeError, IndexError) as error:
        raise mismatch() from error

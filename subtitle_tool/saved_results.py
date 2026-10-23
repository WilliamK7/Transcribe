from __future__ import annotations

import json
import re
from pathlib import Path

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


def normalize_raw(cache, manifest, segment, *, version=None):
    version = version if version is not None else manifest.get("contract", {}).get("normalizer", 1)
    return normalize(read_json(raw_path(cache, segment)), segment_offset(manifest) + segment["offset_ms"], segment["id"],
                     duration_ms=segment.get("duration_ms"), preserve_sentences=bool(manifest.get("subtitle_version")),
                     subtitle_version=manifest.get("subtitle_version", 0), normalizer_version=version)


def _proof(cache, manifest):
    name = manifest.get("normalized_file")
    if name:
        if not isinstance(name, str) or Path(name).name != name:
            raise mismatch()
        path = cache / name
        if path.exists():
            proof = read_json(path)
            if not isinstance(proof, list):
                raise mismatch()
            return proof
    proof = []
    version = manifest.get("normalized_version", manifest.get("contract", {}).get("normalizer", 1))
    for segment in manifest["segments"]:
        items, _ = normalize_raw(cache, manifest, segment, version=version)
        proof.extend(items)
    return proof


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
    version = manifest.get("normalized_version", manifest.get("contract", {}).get("normalizer", 1))
    if version >= 3 and "duration_ms" in segment:
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


def validate_normalized(manifest, cache, sources, cues, references):
    """Prove DB completeness before reviewing, translating or publishing saved work."""
    try:
        from .mt_fallback import validate_origins
        validate_origins(manifest, cues + references)
        validate_segments(manifest)
        records = sources if manifest.get("subtitle_version") else cues
        if "normalized_count" in manifest:
            count = manifest["normalized_count"]
            if type(count) is not int or count != len(records):
                raise mismatch()
        else:
            proof = _proof(cache, manifest)
            key = "sentence_id" if manifest.get("subtitle_version") else "cue_id"
            expected = {record[key]: record for record in proof}
            if len(expected) != len(proof) or len(records) != len(proof):
                raise mismatch()
            for record in records:
                old = expected.get(record[key])
                if old is None or any(record.get(k) != old.get(k) for k in
                                      [key, "cue_id", "text", "language", "begin_ms", "end_ms", "words", "original_timings"]):
                    raise mismatch()
        if not manifest.get("subtitle_version"):
            if sources or references or len({c["cue_id"] for c in cues}) != len(cues):
                raise mismatch()
            segment_ids = {s["id"] for s in manifest["segments"]}
            for cue in cues:
                _validate_record(cue)
                if cue["cue_id"].split(":")[0] not in segment_ids:
                    raise mismatch()
            return
        by_id = {s["sentence_id"]: s for s in sources}
        if len(by_id) != len(sources):
            raise mismatch()
        segments = {s["id"]: s for s in manifest["segments"]}
        grouped, ref_groups = {}, {}
        for source in sources:
            _validate_source(source, segments, manifest)
        for collection, groups, reference_stage in [(cues, grouped, False), (references, ref_groups, True)]:
            if len({c["cue_id"] for c in collection}) != len(collection):
                raise mismatch()
            for cue in collection:
                source = by_id.get(cue.get("sentence_id"))
                if source is None or reference_stage and source["decision"] != "translate":
                    raise mismatch()
                validate_fragment(source, cue, reference_stage=reference_stage)
                groups.setdefault(source["sentence_id"], []).append(cue)
        for source in sources:
            validate_unit_coverage(source, sorted(grouped.get(source["sentence_id"], []), key=lambda c: c["char_start"]))
            if manifest.get("references_planned") and source["decision"] == "translate":
                validate_unit_coverage(source, sorted(ref_groups.get(source["sentence_id"], []), key=lambda c: c["char_start"]),
                                       reference_stage=True)
        if (references and not manifest.get("references_planned") or
                manifest.get("layout_planned") and not manifest.get("references_planned")):
            raise mismatch()
    except (KeyError, ValueError, TypeError, IndexError) as error:
        raise mismatch() from error

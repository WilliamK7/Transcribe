from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, replace
from pathlib import Path

from .api import Bailian, upload_limit
from .common import (ASR_MODEL, MT_MODEL, NORMALIZER_VERSION, PROCESSING_VERSION, SUBTITLE_VERSION,
                     TRANSLATION_STRATEGY_VERSION, MAX_LAYOUT_ROUNDS, Cancelled, NeedsReview,
                     SubmissionUnknown, ToolError, atomic_json, check_cancel, connection_identity, fingerprint)
from .config import Config, decrypt_legacy
from .media import EXTRACT_RECIPE_VERSION, Media
from .store import Store
from .subtitles import (layout_warnings, make_fragment, mt_cache_key, normalize, plan_fragments,
                        plan_reference_units, reference_sources, render_srt, translation_context,
                        validate_fragment, validate_mt_input, wrap, write_srt)


class LocalStateUnavailable(ToolError):
    def __init__(self):
        super().__init__("无法确认本地事务的已提交状态；已停止处理，请检查数据库后恢复。", "local_persistence")


class Engine:
    def __init__(self, config: Config, store: Store, cancel=None, progress=lambda *args: None):
        self.config, self.store = config, store
        self.cancel = cancel or threading.Event()
        self.progress = progress

    def commit_local(self, job_id, manifest, candidate, operation, *args, **kwargs):
        """Only local DB boundaries reload state; cloud facts keep their existing handling."""
        try:
            operation(job_id, *args, candidate, **kwargs)
            manifest.clear()
            manifest.update(candidate)
        except BaseException:
            # An interrupt may land after COMMIT but before the operation returns.
            # Neither the old nor the candidate copy is authoritative in that window.
            try:
                committed = self.store.job(job_id)["manifest"]
                manifest.clear()
                manifest.update(committed)
            except BaseException as error:
                raise LocalStateUnavailable() from error
            raise

    def identity(self, job_id, manifest, *, persist=True):
        identity = connection_identity(manifest)
        version = manifest.get("processing_version", 1)
        if version not in {1, PROCESSING_VERSION}:
            raise ToolError("任务的媒体处理版本不受支持。", "cache_version")
        key = self.config.get_key()
        account = hashlib.sha256(key.encode()).hexdigest()
        if manifest.get("account") and manifest["account"] != account:
            raise ToolError("此任务的 Key 已变更。请恢复原账户 Key；不能跨账户复用任务或缓存。", "account_changed")
        scope = {**identity, "account": account, "processing_version": version}
        if manifest.get("cache_identity") and manifest["cache_identity"] != scope:
            raise ToolError("任务缓存的账号、工作空间或处理版本不一致。", "cache_mismatch")
        for segment in manifest["segments"]:
            if segment.get("upload_identity") and segment["upload_identity"] != scope:
                raise ToolError("分段的账号／工作空间身份与任务不一致。", "cache_mismatch")
        plan = manifest.get("media_plan")
        if plan and (type(plan.get("extract_recipe_version", 1)) is not int or
                     plan.get("extract_recipe_version", 1) not in {1, EXTRACT_RECIPE_VERSION}):
            raise ToolError("媒体提取配方版本不受支持。", "cache_version")
        if plan and (plan.get("version") != version or plan.get("track") != manifest.get("track") or
                     plan.get("channel") != manifest.get("channel")):
            raise ToolError("媒体缓存的版本／音轨／声道与任务记录不一致。", "cache_mismatch")
        manifest.update(identity)
        manifest["account"], manifest["cache_identity"] = account, scope
        if persist:
            self.store.update(job_id, manifest=manifest)
        return key, scope

    def reflow(self, job_id, *, overwrite=False, output_dir=None):
        """Prepare a recoverable local reflow; only run() may translate and commit SRT."""
        job = self.store.job(job_id)
        manifest = job["manifest"]
        output = self.store.resolve_output(job, output_dir)
        self.store.check_reflow_output(job_id, output, overwrite)
        self.identity(job_id, manifest, persist=False)
        self.validate_subtitle_settings(manifest)
        self.config.settings.validate()
        contract = {"asr": ASR_MODEL, "mt": MT_MODEL, "normalizer": NORMALIZER_VERSION,
                    "parameters": {"channel_id": [0], "enable_words": True, "enable_itn": False}}
        saved = manifest.get("contract")
        if saved and (saved.get("normalizer") not in {1, 2, NORMALIZER_VERSION} or
                      {k: v for k, v in saved.items() if k != "normalizer"} !=
                      {k: v for k, v in contract.items() if k != "normalizer"}):
            raise ToolError("本地转写的模型／参数契约不受支持，不能重排。", "cache_version")
        cache = self.config.data / "cache" / job_id
        offset = manifest.get("media_plan", {}).get("audio_offset_ms", manifest.get("audio_offset_ms", 0))
        if type(offset) is not int:
            raise ToolError("本地转写缺少有效的时间偏移记录。", "cache_mismatch")
        if not manifest["segments"]:
            raise ToolError("重排缺少完整本地转写；不会重新上传或识别。", "reflow_cache_missing")
        sources, warnings = [], []
        for segment in manifest["segments"]:
            check_cancel(self.cancel)
            segment_id = segment.get("id", "")
            if not isinstance(segment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", segment_id):
                raise ToolError("转写分段标识无效。", "cache_mismatch")
            path = cache / ("raw-" + segment_id + ".json")
            if not path.is_file():
                raise ToolError("重排缺少完整本地转写；不会重新上传或识别。", "reflow_cache_missing")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ToolError("重排无法读取本地转写，原任务及字幕已保留。", "reflow_cache_invalid") from error
            items, notes = normalize(raw, offset + segment["offset_ms"], segment_id,
                                     duration_ms=segment.get("duration_ms"), preserve_sentences=True,
                                     subtitle_version=SUBTITLE_VERSION, normalizer_version=NORMALIZER_VERSION)
            sources.extend(items)
            warnings.extend(notes)
        sources.sort(key=lambda s: (s["begin_ms"], s["end_ms"], s["sentence_id"]))
        self.preserve_review_decisions(sources, self.store.sources(job_id), self.store.cues(job_id))
        token = str(time.time_ns())
        previous_version = manifest.get("subtitle_version", 0)
        snapshot = self.config.root / ".build" / "reflow" / job_id / (token + ".json")
        self.store.snapshot(job_id, snapshot)
        for name in ["references_planned", "layout_planned", "layout_round", "layout_complete",
                     "expected_srt_sha256", "backup", "cue_count", "output_settings", "no_speech"]:
            manifest.pop(name, None)
        normalized_file = f"subtitle-sources-v{SUBTITLE_VERSION}-reflow-{token}.json"
        manifest.update(subtitle_version=SUBTITLE_VERSION, translation_strategy_version=TRANSLATION_STRATEGY_VERSION,
                        subtitle_settings={k: getattr(self.config.settings, k) for k in
                                           ["zh_line_width", "other_line_width", "chinese_first"]},
                        normalized=True, normalized_count=len(sources), normalized_version=NORMALIZER_VERSION, normalized_file=normalized_file,
                        warnings=warnings, overwrite=bool(overwrite), contract=contract,
                        reflow={"snapshot": str(snapshot.relative_to(self.config.root)), "prepared_at": time.time(),
                                "from_subtitle_version": previous_version})
        atomic_json(cache / normalized_file, sources)
        check_cancel(self.cancel)
        self.store.commit_reflow(job_id, sources, [make_fragment(s) for s in sources], manifest, output)

    @staticmethod
    def preserve_review_decisions(sources, previous_sources, previous_cues):
        previous = {s["sentence_id"]: s for s in previous_sources}
        accounted = set()
        compact = lambda value: re.sub(r"\s", "", value)
        for source in sources:
            sentence_id = source["sentence_id"]
            old = previous.get(sentence_id)
            cues = [c for c in previous_cues if c.get("sentence_id", ":".join(c["cue_id"].split(":")[:2])) == sentence_id]
            manual = [c for c in cues if c["reason"] == "用户在终端确认"]
            if old and old["reason"] == "用户在终端确认":
                if any(old.get(k) != source.get(k) for k in ["text", "language", "original_timings"]):
                    raise ToolError("本地转写与人工语种决定的完整句来源不一致，原任务已保留。", "reflow_review")
                decision = old["decision"], old.get("source_lang", "auto")
                if any((c["decision"], c["source_lang"]) != decision for c in manual):
                    raise ToolError("同一完整句的人工语种决定不一致，原任务已保留。", "reflow_review")
            elif manual:
                decisions = {(c["decision"], c["source_lang"]) for c in cues}
                if (len(decisions) != 1 or compact("".join(c["text"] for c in cues)) != compact(source["text"]) or
                        any(c["language"] != source["language"] for c in cues)):
                    raise ToolError("旧片段的人工语种决定无法完整对应原句，原任务已保留。", "reflow_review")
                decision = next(iter(decisions))
            else:
                continue
            source.update(decision=decision[0], source_lang=decision[1], reason="用户在终端确认")
            accounted.add(sentence_id)
        expected = {s["sentence_id"] for s in previous_sources if s["reason"] == "用户在终端确认"}
        expected.update(c.get("sentence_id", ":".join(c["cue_id"].split(":")[:2])) for c in previous_cues
                        if c["reason"] == "用户在终端确认")
        if not expected.issubset(accounted):
            raise ToolError("部分人工语种决定缺少对应完整句，原任务已保留。", "reflow_review")

    def validate_subtitle_settings(self, manifest):
        settings = self.config.settings
        version = manifest.get("subtitle_version", 0)
        if version:
            if (version not in {1, SUBTITLE_VERSION} or
                    manifest.get("translation_strategy_version") not in {1, 2} or
                    set(manifest.get("subtitle_settings", {})) != {"zh_line_width", "other_line_width", "chinese_first"}):
                raise ToolError("任务的字幕／翻译策略版本或排版快照不受支持。", "cache_version")
            settings = replace(settings, **manifest["subtitle_settings"])
            settings.validate()
        return settings

    def upgrade_translation_strategy(self, job_id, manifest):
        # Settings and identity are validated before upgrading the saved policy.
        if manifest.get("subtitle_version") and manifest["translation_strategy_version"] == 1:
            manifest["translation_strategy_version"] = TRANSLATION_STRATEGY_VERSION
            self.store.update(job_id, manifest=manifest)

    @staticmethod
    def prepare_output(output):
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, test_name = tempfile.mkstemp(prefix=".subtitle-write-test-", dir=output.parent)
            os.close(fd)
            Path(test_name).unlink()
        except OSError as error:
            raise ToolError("视频输出目录不可写；请用 --output-dir 明确选择可写位置。", "output_readonly") from error

    def select_input(self, job_id, track=None, channel=None):
        job = self.store.job(job_id)
        manifest = job["manifest"]
        changes = {k: v for k, v in {"track": track, "channel": channel}.items()
                   if v is not None and manifest.get(k) != v}
        if not changes:
            return
        cache = self.config.data / "cache" / job_id
        if (manifest.get("normalized") or any(s.get("task_id") or s.get("submitting") or s.get("raw")
                for s in manifest["segments"]) or (cache.exists() and any(cache.glob("raw-*.json")))):
            raise ToolError("已提交或提交结果未知的任务不能改变音轨／声道；请保留原任务并新建任务。", "input_committed")
        manifest.update(changes)
        manifest["segments"] = []
        for key in ["media_plan", "audio_offset_ms", "upload_limit_bytes"]:
            manifest.pop(key, None)
        self.cleanup_audio(cache)
        self.store.replace_cues(job_id, [])
        self.store.update(job_id, manifest=manifest)

    def prepare_audio(self, media, source, cache, manifest, limit, force_method=None, *, recipe=None):
        if manifest.get("processing_version", 1) == 1:
            full = cache / "audio.wav"
            media.extract(source, full, manifest["track"], manifest["channel"], info=manifest["media"])
            segments = media.split(full, cache, limit)
            if len(segments) > 1:
                full.unlink(missing_ok=True)
            return None, segments
        return media.prepare(source, cache, manifest["media"], manifest["track"], manifest["channel"],
                             limit, force_method=force_method,
                             extract_recipe_version=(recipe if recipe is not None else
                                 manifest["media_plan"].get("extract_recipe_version", 1) if manifest.get("media_plan")
                                 else EXTRACT_RECIPE_VERSION))

    def rebuild_audio(self, media, source, cache, manifest, job_id):
        original = manifest.get("media_plan")
        recipe = original.get("extract_recipe_version", 1) if original else EXTRACT_RECIPE_VERSION
        upgrade = (original and recipe == 1 and original.get("method") == "copy" and
                   original.get("container") == "ogg" and original.get("codec") in {"opus", "vorbis"} and
                   not manifest.get("normalized") and
                   not any(s.get("task_id") or s.get("submitting") or s.get("raw") for s in manifest["segments"]) and
                   not any(cache.glob("raw-*.json")))
        if upgrade and manifest.get("fingerprint") != fingerprint(source, self.cancel):
            raise ToolError("旧 OGG 缺少可确认的源文件身份，不能升级提取配方。", "cache_mismatch")
        with tempfile.TemporaryDirectory(prefix=".rebuild-", dir=cache) as directory:
            prepared = Path(directory)
            plan, rebuilt = self.prepare_audio(media, source, prepared, manifest,
                    manifest["upload_limit_bytes"], original["method"] if original else None,
                    recipe=EXTRACT_RECIPE_VERSION if upgrade else recipe)
            self.validate_rebuilt(original, manifest["segments"], plan, rebuilt, upgrade=upgrade)
            check_cancel(self.cancel)
            if upgrade and manifest["fingerprint"] != fingerprint(source, self.cancel):
                raise ToolError("准备期间源文件发生变化，旧 OGG 身份未升级。", "source_changed")
            for segment in rebuilt:
                os.replace(prepared / segment["audio"], cache / segment["audio"])
            if upgrade:
                candidate = copy.deepcopy(manifest)
                candidate["media_plan"] = plan
                for old, new in zip(candidate["segments"], rebuilt):
                    old.update(new)
                    for name in ["oss_url", "oss_protected", "upload_expires", "upload_identity"]:
                        old.pop(name, None)
                self.commit_local(job_id, manifest, candidate,
                                  lambda jid, value: self.store.update(jid, manifest=value))

    @staticmethod
    def validate_rebuilt(original, segments, plan, rebuilt, *, upgrade=False):
        fields = ("id", "audio", "offset_ms", "duration_ms", "start_frame", "sample_count", "sample_rate", "codec", "container", "bytes", "sha256")
        ignored = {"bytes", "sha256"} if upgrade else set()
        matches = all(all(old.get(k) == new.get(k) for k in fields if k in old and k not in ignored)
                      for old, new in zip(segments, rebuilt))
        ignored_plan = {"reason", "extract_recipe_version"} if upgrade else {"reason"}
        expected = {**(original or {}), "extract_recipe_version": (original or {}).get("extract_recipe_version", 1)}
        plan_matches = (not original or {k: v for k, v in plan.items() if k not in ignored_plan} ==
                        {k: v for k, v in expected.items() if k not in ignored_plan})
        if len(rebuilt) != len(segments) or not matches or not plan_matches:
            raise ToolError("恢复提取的编码、分段或时间轴与原记录不同，不能改变已有识别输入。", "cache_mismatch")

    def run(self, job_id):
        job = self.store.job(job_id)
        if job["state"] in {"completed", "skipped", "cancelled"}:
            return job["state"]
        if job["state"] == "submission_unknown":
            return "submission_unknown"
        manifest = job["manifest"]
        cache = self.config.data / "cache" / job_id
        source = Path(job["source"])
        api, key, scope = None, None, None
        started = time.monotonic()
        stage_started, current_stage = started, None
        stage_times = {}
        handling_failure = False

        def measure_stage():
            nonlocal stage_started
            now = time.monotonic()
            if current_stage:
                stage_times[current_stage] = stage_times.get(current_stage, 0) + now - stage_started
            stage_started = now

        def stage(name, done=None, total=None):
            nonlocal current_stage
            if name != current_stage:
                measure_stage()
                current_stage = name
            self.store.update(job_id, name, manifest)
            self.progress(job_id, name, done, total)

        def cloud_progress(name, done, total):
            if name == "mt_cooldown":
                self.store.count(job_id, "mt_cooldown_ms", round(done * 1000))
            self.progress(job_id, name, done, total)

        def client():
            nonlocal api
            if api is None:
                api = Bailian(key, manifest["region"], self.cancel,
                    count=lambda name: self.store.count(job_id, name),
                    progress=cloud_progress,
                    workspace_id=manifest["workspace_id"], access_mode=manifest["access_mode"])
            return api

        def save_failure(state, error, code, request_id=""):
            nonlocal handling_failure
            handling_failure = True
            # A late interrupt/cleanup error cannot undo a committed success.
            # Do not reload the shared manifest here: it may contain a newly received task_id.
            if self.store.job(job_id)["state"] != "completed":
                self.store.update(job_id, state, manifest, error, code, request_id)

        try:
            check_cancel(self.cancel)
            output = self.store.ensure_output(job_id)
            cache.mkdir(parents=True, exist_ok=True)
            subtitle_version = manifest.get("subtitle_version", 0)
            settings = self.validate_subtitle_settings(manifest)
            if output.exists() and not manifest["overwrite"]:
                if manifest.get("expected_srt_sha256") == hashlib.sha256(output.read_bytes()).hexdigest():
                    stage("completed")
                    return "completed"
                stage("skipped")
                return "skipped"
            if manifest.get("reflow"):
                if subtitle_version != SUBTITLE_VERSION or not manifest.get("normalized"):
                    raise ToolError("重排的本地来源／恢复标记不完整。", "cache_mismatch")
                self.prepare_output(output)
                key, scope = self.identity(job_id, manifest)
                self.upgrade_translation_strategy(job_id, manifest)
                return self.finish_subtitles(job_id, manifest, settings, scope, client, stage, output, cache)
            stage("probing")
            if not source.is_file():
                raise ToolError("源文件已移动或删除；请重新选择文件。", "input")
            self.prepare_output(output)
            current = fingerprint(source, self.cancel)
            if manifest.get("fingerprint") and manifest["fingerprint"] != current:
                raise ToolError("源文件内容或时间发生变化；缓存未复用。请停止此任务后重新入队。", "source_changed")
            manifest["fingerprint"] = current
            contract = {"asr": ASR_MODEL, "mt": MT_MODEL, "normalizer": NORMALIZER_VERSION,
                        "parameters": {"channel_id": [0], "enable_words": True, "enable_itn": False}}
            if manifest.get("contract") and manifest["contract"] != contract:
                saved = manifest["contract"]
                compatible = (type(saved.get("normalizer")) is int and saved["normalizer"] in {1, 2, NORMALIZER_VERSION} and
                              {k: v for k, v in saved.items() if k != "normalizer"} ==
                              {k: v for k, v in contract.items() if k != "normalizer"})
                if not compatible:
                    raise ToolError("缓存接口／断句版本不受支持；请检查任务版本。", "cache_version")
                contract = saved
                if saved["normalizer"] == 1 and not manifest.get("normalized"):
                    contract = {**saved, "normalizer": 2}
                    manifest["normalization_upgraded_from"] = 1
            manifest["contract"] = contract
            normalizer_version = contract["normalizer"]
            # Even normalized/translated cache hits must validate the current account and saved endpoint.
            key, scope = self.identity(job_id, manifest)
            self.upgrade_translation_strategy(job_id, manifest)
            media = Media(self.config.root, self.cancel)
            if not manifest.get("media"):
                manifest["media"] = media.probe(source)
            track, audio_offset = media.select(manifest["media"], manifest.get("track"),
                                               manifest.get("channel", settings.channel))
            manifest["track"], manifest["audio_offset_ms"] = track, audio_offset
            if manifest.get("media_plan"):
                manifest["audio_offset_ms"] = manifest["media_plan"]["audio_offset_ms"]
            manifest.setdefault("channel", settings.channel)
            self.store.update(job_id, manifest=manifest)
            if not manifest["segments"] and not manifest.get("normalized"):
                stage("uploading")
                policy = client().policy()
                limit = upload_limit(policy)
                manifest["upload_limit_bytes"] = limit
                stage("extracting")
                plan, manifest["segments"] = self.prepare_audio(media, source, cache, manifest, limit)
                if plan:
                    manifest["media_plan"] = plan
                    manifest["audio_offset_ms"] = audio_offset = plan["audio_offset_ms"]
                self.store.update(job_id, manifest=manifest)
            if manifest.get("media_plan"):
                audio_offset = manifest["media_plan"]["audio_offset_ms"]
                self.progress(job_id, "audio_ready", {**manifest["media_plan"],
                              "segments": len(manifest["segments"]),
                              "bytes": sum(s.get("bytes", 0) for s in manifest["segments"])}, None)
            position, replans = 0, 0
            while position < len(manifest["segments"]):
                segment = manifest["segments"][position]
                check_cancel(self.cancel)
                if segment.get("upload_identity") and segment["upload_identity"] != scope:
                    raise ToolError("分段的上传／识别身份与任务记录不一致。", "cache_mismatch")
                if manifest.get("normalized"):
                    break
                raw_path = cache / ("raw-" + segment["id"] + ".json")
                if raw_path.exists():
                    position += 1
                    continue
                if segment.get("submitting") and not segment.get("task_id"):
                    raise SubmissionUnknown()
                if not segment.get("task_id"):
                    audio = cache / segment["audio"]
                    oss_url = None
                    if segment.get("upload_expires", 0) > time.time() + 60:
                        oss_url = segment.get("oss_url")
                        if not oss_url and segment.get("oss_protected"):
                            try:
                                oss_url = decrypt_legacy(base64.b64decode(segment["oss_protected"])).decode()
                            except (ToolError, ValueError, UnicodeError):
                                # No task or unknown marker: safe to upload again, never to repeat a POST.
                                oss_url = None
                    if oss_url and (not isinstance(oss_url, str) or not oss_url.startswith("oss://") or any(c.isspace() for c in oss_url)):
                        raise ToolError("缓存上传引用格式无效。", "cache_mismatch")
                    if not oss_url:
                        if (not audio.exists() or (segment.get("sha256") and
                                media.audio_identity(audio) != {"bytes": segment["bytes"], "sha256": segment["sha256"]})):
                            stage("extracting")
                            self.rebuild_audio(media, source, cache, manifest, job_id)
                            # A legacy recipe upgrade replaces the segment dictionaries at commit.
                            segment = manifest["segments"][position]
                            audio = cache / segment["audio"]
                        stage("uploading")
                        try:
                            oss_url = client().upload(audio)
                        except ToolError as error:
                            committed = (any(s.get("task_id") or s.get("submitting") or s.get("raw") for s in manifest["segments"])
                                         or any(cache.glob("raw-*.json")))
                            if error.code != "upload_size" or committed or replans >= 2:
                                raise
                            replans += 1
                            manifest["upload_limit_bytes"] = error.limit
                            stage("extracting")
                            self.cleanup_audio(cache)
                            plan, manifest["segments"] = self.prepare_audio(media, source, cache, manifest, error.limit, "flac")
                            if plan:
                                manifest["media_plan"] = plan
                                manifest["audio_offset_ms"] = audio_offset = plan["audio_offset_ms"]
                            self.store.update(job_id, manifest=manifest)
                            position = 0
                            continue
                        segment["oss_url"] = oss_url
                        segment.pop("oss_protected", None)
                        segment["upload_identity"] = scope
                        segment["upload_expires"] = time.time() + 48 * 3600
                        self.store.update(job_id, manifest=manifest)
                    # Construct/check client BEFORE the durable marker; only POST uncertainty should mark it.
                    cloud = client()
                    segment["submitting"] = True
                    stage("submitting")
                    try:
                        task_id, request_id = cloud.submit(oss_url)
                    except (SubmissionUnknown, Cancelled):
                        raise
                    except ToolError:
                        # An explicit 4xx rejection is known not to have created the task.
                        segment["submitting"] = False
                        self.store.update(job_id, manifest=manifest)
                        raise
                    segment["task_id"] = task_id
                    segment["submitted_at"] = time.time()
                    segment["submitting"] = False
                    self.store.update(job_id, "recognizing", manifest, request_id=request_id)
                stage("recognizing")
                deadline = time.monotonic() + settings.poll_timeout_seconds
                while True:
                    check_cancel(self.cancel)
                    result = client().query(segment["task_id"])
                    cloud_output = result.get("output")
                    if not isinstance(cloud_output, dict):
                        raise ToolError("查询缺少合法的 output 对象；已保存 task_id。", "response_schema")
                    status = cloud_output.get("task_status")
                    segment["cloud_status"] = status
                    self.store.update(job_id, manifest=manifest)
                    self.progress(job_id, "recognizing", status, None)
                    if status == "SUCCEEDED":
                        cloud_result = cloud_output.get("result")
                        url = cloud_result.get("transcription_url") if isinstance(cloud_result, dict) else None
                        if not isinstance(url, str) or not url:
                            raise ToolError("任务成功但缺少 output.result.transcription_url。", "response_schema", result.get("request_id", ""))
                        stage("downloading_result")
                        raw = client().download(url)
                        # The exact response is preserved; signed result URLs are never persisted.
                        atomic_json(raw_path, raw)
                        segment["raw"] = raw_path.name
                        self.store.update(job_id, manifest=manifest)
                        break
                    if status in {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}:
                        code = result.get("output", {}).get("code", "asr_" + status.lower())
                        raise ToolError(f"云端识别 {status}（{code}）。如结果已过期，请新建任务；不会自动重新计费。",
                                        code, result.get("request_id", ""))
                    if status not in {"PENDING", "RUNNING"}:
                        raise ToolError("查询返回未知状态；已保存 task_id，可重试查询。", "response_schema")
                    if time.monotonic() >= deadline:
                        raise ToolError("本地轮询超时；已保存 task_id，重试时只查询原任务。", "poll_timeout")
                    if self.cancel.wait(settings.poll_seconds):
                        check_cancel(self.cancel)
                position += 1
            if not manifest.get("normalized"):
                stage("segmenting")
                candidate = copy.deepcopy(manifest)
                cues = []
                for segment in manifest["segments"]:
                    raw = json.loads((cache / ("raw-" + segment["id"] + ".json")).read_text(encoding="utf-8"))
                    items, warnings = normalize(raw, audio_offset + segment["offset_ms"], segment["id"],
                                                duration_ms=segment.get("duration_ms"), preserve_sentences=bool(subtitle_version),
                                                subtitle_version=subtitle_version, normalizer_version=normalizer_version)
                    cues.extend(items)
                    candidate["warnings"].extend(warnings)
                cues.sort(key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))
                normalized_file = (f"subtitle-sources-v{subtitle_version}.json" if subtitle_version else
                                   f"normalized-v{normalizer_version}.json")
                atomic_json(cache / normalized_file, cues)
                candidate.update(normalized=True, normalized_count=len(cues),
                                 normalized_version=normalizer_version, normalized_file=normalized_file)
                sources, fragments = (cues, [make_fragment(s) for s in cues]) if subtitle_version else ([], cues)
                self.commit_local(job_id, manifest, candidate, self.store.initialize_subtitles, sources, fragments)
            return self.finish_subtitles(job_id, manifest, settings, scope, client, stage, output, cache)
        except LocalStateUnavailable:
            # Never let a generic handler persist an unconfirmed in-memory state.
            raise
        except (Cancelled, KeyboardInterrupt) as e:
            self.cancel.set()
            e = Cancelled()
            ambiguous = (not manifest.get("reflow") and
                         any(s.get("submitting") and not s.get("task_id") for s in manifest["segments"]))
            save_failure("submission_unknown" if ambiguous else "cancelled",
                         str(e), "submission_unknown" if ambiguous else e.code)
        except SubmissionUnknown as e:
            save_failure("submission_unknown", str(e), e.code, e.request_id)
        except NeedsReview as e:
            save_failure("needs_review", str(e), e.code, e.request_id)
        except ToolError as e:
            save_failure("skipped" if e.code == "exists" else "failed", str(e), e.code, e.request_id)
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Exception text may contain transcripts or credentials; only class is reported.
            save_failure("failed", "本地数据／文件操作失败（" + type(e).__name__ +
                         "）。请检查磁盘空间、权限或缓存结构。", "local_io")
        finally:
            primary_failure = handling_failure or sys.exc_info()[0] is not None
            try:
                measure_stage()
                for name, seconds in stage_times.items():
                    self.store.count(job_id, "stage_" + name + "_ms", round(seconds * 1000))
                self.store.count(job_id, "run_ms", round((time.monotonic() - started) * 1000))
            except (OSError, sqlite3.Error, Cancelled, KeyboardInterrupt):
                if not primary_failure:
                    raise
        state = self.store.job(job_id)["state"]
        self.progress(job_id, state, None, None)
        return state

    def confirm_empty_normalization(self, job_id, manifest, cache):
        if (not manifest.get("normalized") or
                manifest.get("subtitle_version") and self.store.sources(job_id)):
            raise ToolError("字幕记录缺失，不能确认无语音结果。", "cache_mismatch")
        version = manifest.get("normalized_version", manifest.get("contract", {}).get("normalizer", 1))
        if type(version) is not int or version not in {1, 2, NORMALIZER_VERSION}:
            raise ToolError("规范化版本不受支持。", "cache_version")
        if "normalized_count" in manifest:
            if type(manifest["normalized_count"]) is int and manifest["normalized_count"] == 0:
                return
            raise ToolError("字幕记录与规范化数量不一致。", "cache_mismatch")
        # Legacy empty tasks need independent evidence; an empty DB alone proves nothing.
        try:
            name = manifest.get("normalized_file")
            if name:
                if not isinstance(name, str) or Path(name).name != name:
                    raise ValueError()
                path = cache / name
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(saved, list) or saved:
                        raise ValueError()
                    return
            if not manifest["segments"]:
                raise ValueError()
            for segment in manifest["segments"]:
                segment_id = segment["id"]
                if not isinstance(segment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", segment_id):
                    raise ValueError()
                raw = json.loads((cache / ("raw-" + segment_id + ".json")).read_text(encoding="utf-8"))
                offset = manifest.get("media_plan", {}).get("audio_offset_ms", manifest.get("audio_offset_ms", 0))
                items, _ = normalize(raw, offset + segment["offset_ms"], segment_id,
                                     duration_ms=segment.get("duration_ms"),
                                     preserve_sentences=bool(manifest.get("subtitle_version")),
                                     subtitle_version=manifest.get("subtitle_version", 0), normalizer_version=version)
                if items:
                    raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, ToolError) as error:
            raise ToolError("旧任务缺少一致的空规范化证据；请检查本地缓存，不会重新提交识别。", "cache_mismatch") from error

    def finish_subtitles(self, job_id, manifest, settings, scope, client, stage, output, cache):
        cues = self.store.cues(job_id)
        if not cues:
            self.confirm_empty_normalization(job_id, manifest, cache)
            manifest["no_speech"] = True
            stage("completed")
            if not manifest.get("reflow"):
                self.cleanup_completed_audio(job_id, manifest, cache)
            return "completed"
        if any(c["decision"] == "uncertain" for c in cues):
            raise NeedsReview("部分字幕语种不确定，请用 --review 逐条确认原文或翻译。", "language_uncertain")
        if manifest.get("subtitle_version"):
            self.process_subtitles(job_id, manifest, settings, scope, client, stage)
        else:
            stage("translating")
            self.translate(job_id, cues, scope, client)
        cues = self.store.cues(job_id)
        check_cancel(self.cancel)
        stage("writing")
        content = render_srt(cues, settings, subtitle_version=manifest.get("subtitle_version", 0))
        manifest["expected_srt_sha256"] = hashlib.sha256(content).hexdigest()
        self.store.update(job_id, manifest=manifest)
        manifest["backup"] = write_srt(output, content, manifest["overwrite"])
        manifest["cue_count"] = len(cues)
        manifest["output_settings"] = asdict(settings)
        stage("completed")
        if not manifest.get("reflow"):
            self.cleanup_completed_audio(job_id, manifest, cache)
        return "completed"

    def process_subtitles(self, job_id, manifest, settings, scope, client, stage):
        sources = self.store.sources(job_id)
        if not sources:
            raise ToolError("新任务缺少完整句来源记录，不能改用旧断句规则恢复。", "cache_mismatch")
        if not manifest.get("references_planned"):
            references = plan_reference_units(sources)
            candidate = copy.deepcopy(manifest)
            candidate["references_planned"] = True
            self.commit_local(job_id, manifest, candidate, self.store.save_layout,
                              self.store.cues(job_id), references=references)
        stage("reference_translating")
        references = self.store.references(job_id)
        source_ids = {s["sentence_id"]: s for s in sources}
        for reference in references:
            if reference.get("sentence_id") not in source_ids:
                raise ToolError("参考翻译缺少对应完整句来源。", "cache_mismatch")
            validate_fragment(source_ids[reference["sentence_id"]], reference, reference_stage=True)
        self.translate(job_id, references, scope, client, stage_name="reference_translating",
                       reference_stage=True, strategy_version=manifest["translation_strategy_version"])
        references = self.store.references(job_id)
        sources = reference_sources(sources, references)
        # Both references and cues are validated against the same source and word ranges.
        by_range = {(r["sentence_id"], r["word_start"], r["word_end"]): r for r in references}
        if not manifest.get("layout_planned"):
            stage("layout_adjusting", 0, MAX_LAYOUT_ROUNDS)
            cues = []
            for source in sources:
                cues.extend(plan_fragments(source, settings, reference=source["reference"]))
            for cue in cues:
                if cue["decision"] == "translate":
                    validate_mt_input(cue["text"], cue["source_lang"])
            candidate = copy.deepcopy(manifest)
            candidate.update(layout_planned=True, layout_round=0)
            self.commit_local(job_id, manifest, candidate, self.store.save_layout, cues)
        while True:
            check_cancel(self.cancel)
            stage("subtitle_translating")
            cues = self.store.cues(job_id)
            contexts = {}
            for cue in cues:
                check_cancel(self.cancel)
                if cue.get("sentence_id") not in source_ids:
                    raise ToolError("字幕片段缺少对应完整句来源。", "cache_mismatch")
                validate_fragment(source_ids[cue["sentence_id"]], cue)
                if cue["decision"] != "translate" or cue["translated"]:
                    continue
                source = source_ids[cue["sentence_id"]]
                exact = by_range.get((cue["sentence_id"], cue["word_start"], cue["word_end"]))
                whole = cue["char_start"] == 0 and cue["char_end"] == len(source["text"])
                context = [] if exact and whole else translation_context(cue, sources)
                if exact and not context:
                    self.store.translation_update(job_id, [cue["cue_id"]], translated=exact["translated"],
                                                  counts={"mt_reference_reused": 1})
                else:
                    contexts[cue["cue_id"]] = context
            self.translate(job_id, self.store.cues(job_id), scope, client, stage_name="subtitle_translating",
                           strategy_version=manifest["translation_strategy_version"], contexts=contexts)
            cues = self.store.cues(job_id)
            if manifest.get("layout_complete"):
                return
            round_number = manifest["layout_round"]
            changed, revised = False, []
            if round_number < MAX_LAYOUT_ROUNDS:
                stage("layout_adjusting", round_number + 1, MAX_LAYOUT_ROUNDS)
                by_id = {s["sentence_id"]: s for s in sources}
                for cue in cues:
                    parts = [cue]
                    if cue["decision"] == "translate" and len(wrap(cue["translated"], settings.zh_line_width * 2)) > 2:
                        parts = plan_fragments(by_id[cue["sentence_id"]], settings, cue=cue,
                                               reference=cue["translated"], layout_round=round_number + 1, force=True)
                    changed |= len(parts) > 1
                    revised.extend(parts)
            if not changed:
                for warning in layout_warnings(cues, settings):
                    if warning not in manifest["warnings"]:
                        manifest["warnings"].append(warning)
                manifest["layout_complete"] = True
                self.store.update(job_id, manifest=manifest)
                return
            candidate = copy.deepcopy(manifest)
            candidate["layout_round"] = round_number + 1
            self.commit_local(job_id, manifest, candidate, self.store.save_layout, revised)

    def translate(self, job_id, cues, scope, client, *, stage_name="translating", reference_stage=False,
                  strategy_version=0, contexts=None):
        cache_stage = "reference" if reference_stage else "subtitle"
        groups, pending, done = {}, [], 0
        for cue in cues:
            if cue["decision"] != "translate" or cue["translated"]:
                done += 1
                continue
            context = (contexts or {}).get(cue["cue_id"], [])
            key = mt_cache_key(cue["text"], cue["source_lang"], **scope, stage=cache_stage,
                               strategy_version=strategy_version, tm_list=context, asr_language=cue["language"].lower().strip())
            groups.setdefault(key, ([], context))[0].append(cue)
        for key, (members, context) in groups.items():
            cached = self.store.cache_get(key)
            if cached:
                self.store.translation_update(job_id, [c["cue_id"] for c in members], reference_stage=reference_stage,
                                              translated=cached,
                                              counts={"mt_" + cache_stage + "_cache_hit": len(members)} if strategy_version else None)
                done += len(members)
            else:
                pending.append((members, key, context))
        self.progress(job_id, stage_name, done, len(cues))
        if not pending:
            return
        cloud = client()
        errors = []
        def translate_group(members, key, context):
            check_cancel(self.cancel)
            cue_ids = [c["cue_id"] for c in members]
            self.store.translation_update(job_id, cue_ids, reference_stage=reference_stage, attempt=True,
                                          counts={"mt_" + cache_stage + "_request": 1} if strategy_version else None)
            cue = members[0]
            check_cancel(self.cancel)
            if strategy_version:
                text, _ = cloud.translate(cue["text"], cue["source_lang"], tm_list=context)
            else:
                text, _ = cloud.translate(cue["text"], cue["source_lang"])
            self.store.translation_update(job_id, cue_ids, reference_stage=reference_stage, translated=text,
                                          cache_key=key, counts={"mt_" + cache_stage + "_completed": len(members)})
        workers = self.config.settings.mt_workers
        with ThreadPoolExecutor(max_workers=workers) as executor:
            remaining = iter(pending)
            futures = {}
            def schedule():
                while len(futures) < workers and not errors and not self.cancel.is_set():
                    item = next(remaining, None)
                    if item is None:
                        break
                    members, key, context = item
                    futures[executor.submit(translate_group, members, key, context)] = members
            schedule()
            while futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    members = futures.pop(future)
                    try:
                        future.result()
                        done += len(members)
                    except ToolError as e:
                        self.store.translation_update(job_id, [c["cue_id"] for c in members],
                                                      reference_stage=reference_stage, error=str(e))
                        errors.append(e)
                    self.progress(job_id, stage_name, done, len(cues))
                # Stop scheduling after a failed request. Keep the at-most-workers in-flight successes.
                schedule()
        check_cancel(self.cancel)
        if errors:
            raise errors[0]

    def cleanup_completed_audio(self, job_id, manifest, cache):
        if self.cleanup_audio(cache, best_effort=True):
            warning = "音频缓存清理未完成；任务和字幕已保存，可稍后释放文件占用。"
            if warning not in manifest["warnings"]:
                manifest["warnings"].append(warning)
            self.store.update(job_id, manifest=manifest)

    @staticmethod
    def cleanup_audio(cache, *, best_effort=False):
        failures = 0
        for suffix in ["wav", "flac", "m4a", "mp3", "ogg"]:
            for path in cache.glob("audio*." + suffix):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    if not best_effort:
                        raise
                    failures += 1
        return failures

    def resolve_unknown(self, job_id, task_id=None, allow_resubmit=False):
        job = self.store.job(job_id)
        manifest = job["manifest"]
        if job["state"] != "submission_unknown":
            raise ToolError("当前任务不是提交未知状态。", "job")
        resolved = False
        for segment in manifest["segments"]:
            if segment.get("submitting") and not segment.get("task_id"):
                if task_id:
                    if not task_id.isascii() or not all(c.isalnum() or c in "-_" for c in task_id) or len(task_id) > 200:
                        raise ToolError("task_id 格式无效。", "input")
                    segment["task_id"] = task_id
                elif not allow_resubmit:
                    raise ToolError("重新提交需要明确确认可能重复计费。", "submission_unknown")
                segment["submitting"] = False
                if not task_id:
                    for name in ["oss_url", "oss_protected", "upload_expires", "upload_identity"]:
                        segment.pop(name, None)
                resolved = True
                break
        if not resolved:
            raise ToolError("未知提交记录缺少对应分段，不能自动重发；请检查任务记录。", "submission_unknown")
        self.store.update(job_id, "queued", manifest)

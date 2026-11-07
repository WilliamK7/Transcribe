from __future__ import annotations

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
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .api import Bailian, upload_limit
from .common import DISPLAY_TIMING_VERSION, NORMALIZER_VERSION, SUBTITLE_VERSION, Cancelled, NeedsReview, SubmissionUnknown, ToolError, atomic_json, check_cancel, connection_identity, fingerprint, output_key
from .config import Config
from .media import Media
from .mt_control import MTControl
from .mt_backend import MTBackend
from .store import TERMINAL, Store
from .review import decision_origin
from .policy import asr_contract, current_execution_snapshot, load_policy
from .saved_results import (mismatch, normalize_raw, raw_path as saved_raw_path, segment_offset,
                            validate_normalized, validate_segments)
from .publication import inspect_intent, prepare_intent, write_srt
from .subtitles import make_fragment, normalize, plan_display_timing, render_srt


class LocalStateUnavailable(ToolError):
    def __init__(self):
        super().__init__("无法确认本地事务的已提交状态；已停止处理，请检查数据库后恢复。", "local_persistence")


def unknown_submission(manifest, state):
    return not manifest.get('reflow') and (
        state == 'submission_unknown' or
        any(s.get('submitting') and not s.get('task_id') for s in manifest.get('segments', [])))


class Engine:
    def __init__(self, config: Config, store: Store, cancel=None, progress=lambda *args: None):
        self.config, self.store = config, store
        self.cancel = cancel or threading.Event()
        self.progress = progress
        self.mt_control = MTControl(self.cancel)
        self.mt_runtime_active = False
        self.run_id = uuid.uuid4().hex

    def request_cancel(self, *, force=False):
        self.mt_control.cancel_user(force=force)

    def recover_jobs(self):
        for job in self.store.jobs():
            if job["state"] in TERMINAL:
                continue
            if unknown_submission(job["manifest"], job["state"]):
                self.store.update(job["id"], "submission_unknown",
                                  error="提交被中断，结果未知；请确认云端任务。", code="submission_unknown")
            elif job["state"] not in {"failed", "needs_review", "queued"}:
                self.store.update(job["id"], "queued")

    def resume(self, job_id, *, track=None, channel=None, output_dir=None, overwrite=False,
               task_id=None, allow_resubmit=False, mt_paid=None):
        """Prepare an explicitly requested resume; run() remains the sole executor."""
        manifest = self.store.job(job_id)['manifest']
        load_policy(manifest)
        from .mt_pool import saved_route
        from .mt_quota import reset_quota_for_resume
        route = saved_route(manifest)
        if mt_paid is not None:
            route['routing']['paid_enabled'] = mt_paid == 'allow'
            manifest['mt_route_snapshot'] = route
            self.store.update(job_id, manifest=manifest)
        for entry in route['entries']:
            if entry['tier'] == 'free' and entry['backend'] is not None:
                backend = MTBackend.from_snapshot(entry['backend'])
                if backend.free_quota:
                    reset_quota_for_resume(backend)
        self.select_input(job_id, track, channel)
        job = self.store.job(job_id)
        if job["state"] in {"completed", "skipped"}:
            if output_dir is not None and output_key(self.store.resolve_output(job, output_dir)) != job["output_key"]:
                raise ToolError("已完成任务的输出不能通过恢复修改，请新建任务。", "job")
            if task_id or allow_resubmit:
                self.resolve_unknown(job_id, task_id, allow_resubmit)
            return
        if overwrite or output_dir is not None:
            self.store.change_output(job_id, output_dir, overwrite)
        if unknown_submission(job["manifest"], job["state"]):
            self.store.update(job_id, "submission_unknown")
        elif job["state"] == "cancelled":
            self.store.update(job_id, "queued")
        if task_id or allow_resubmit:
            self.resolve_unknown(job_id, task_id, allow_resubmit)


    def cancel_job(self, job_id, *, interrupted=False):
        job = self.store.job(job_id)
        if job["state"] in TERMINAL:
            return
        manifest = job["manifest"]
        ambiguous = unknown_submission(manifest, job["state"])
        state = "submission_unknown" if interrupted and ambiguous else "cancelled"
        self.store.update(job_id, state, manifest,
                          error="用户停止本地任务；已提交的云端任务仍可能计费。",
                          code="submission_unknown" if interrupted and ambiguous else "cancelled")

    def review_media(self, job_id):
        job = self.store.job(job_id)
        manifest = job["manifest"]
        if not manifest.get("media"):
            manifest["media"] = Media(self.config.root, self.cancel).probe(Path(job["source"]))
            self.store.update(job_id, manifest=manifest)
        selected = manifest.get("track")
        if job["error_code"] == "audio_channel":
            selected, _ = Media(self.config.root, self.cancel).select(manifest["media"], selected, "mix")
        return manifest["media"], selected

    def finish_review(self, job_id):
        job = self.store.job(job_id)
        if job["state"] != "needs_review":
            raise ToolError("当前任务不是待复核状态。", "review")
        if job["error_code"] == "language_uncertain" and any(c["decision"] == "uncertain" for c in self.store.cues(job_id)):
            raise ToolError("仍有未确认的语种决定。", "review")
        self.store.update(job_id, "queued")

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
        if type(version) is not int or version < 1:
            raise ToolError("任务的媒体处理版本无效。", "cache_version")
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
        if plan and (plan.get("version") != version or plan.get("track") != manifest.get("track") or
                     plan.get("channel") != manifest.get("channel")):
            raise ToolError("媒体缓存的版本／音轨／声道与任务记录不一致。", "cache_mismatch")
        manifest.update(identity)
        manifest["account"], manifest["cache_identity"] = account, scope
        if persist:
            self.store.update(job_id, manifest=manifest)
        return key, scope

    def reflow(self, job_id, *, overwrite=False, output_dir=None, mt_paid=None):
        """Prepare a recoverable local reflow; only run() may translate and commit SRT."""
        job = self.store.job(job_id)
        if job['state'] == 'archived':
            return self.rebuild_archive(job_id, output_dir, overwrite=overwrite, mt_paid=mt_paid)
        manifest = job["manifest"]
        output = self.store.resolve_output(job, output_dir)
        self.store.check_republish_output(job_id, output, overwrite, operation="重排")
        load_policy(manifest, verify_execution=False).settings(self.config.settings)
        self.identity(job_id, manifest, persist=False)
        self.config.settings.validate()
        from .mt_pool import configured_route
        route = configured_route(self.config, manifest, paid_enabled=None if mt_paid is None else mt_paid == 'allow')
        contract = asr_contract()
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
                                     duration_ms=segment.get("duration_ms"))
            sources.extend(items)
            warnings.extend(notes)
        sources.sort(key=lambda s: (s["begin_ms"], s["end_ms"], s["sentence_id"]))
        self.preserve_review_decisions(sources, self.store.sources(job_id), self.store.cues(job_id))
        token = str(time.time_ns())
        previous_version = manifest.get("subtitle_version", 0)
        snapshot = self.config.root / ".build" / "reflow" / job_id / (token + ".json")
        self.store.snapshot(job_id, snapshot)
        for name in ["layout_planned", "layout_round", "layout_complete",
                     "mt_glossary_schedule", "mt_snapshot",
                     "expected_srt_sha256", "publication_intent", "display_timing_adjustments", "retime",
                     "backup", "cue_count", "no_speech"]:
            manifest.pop(name, None)
        manifest.update(subtitle_version=SUBTITLE_VERSION, translation_strategy_version=5,
                        mt_glossary_schedule="prefix-v1",
                        display_timing_version=DISPLAY_TIMING_VERSION,
                        execution_snapshot=current_execution_snapshot(),
                        subtitle_settings={k: getattr(self.config.settings, k) for k in
                                           ["zh_line_width", "other_line_width", "chinese_first"]},
                        normalized=True, normalized_count=len(sources), normalized_version=NORMALIZER_VERSION,
                        warnings=warnings, overwrite=bool(overwrite), contract=contract,
                        reflow={"snapshot": str(snapshot.relative_to(self.config.root)), "prepared_at": time.time(),
                                "from_subtitle_version": previous_version})
        manifest['mt_route_snapshot'] = route
        check_cancel(self.cancel)
        self.store.commit_reflow(job_id, sources, [make_fragment(s) for s in sources], manifest, output)
        return job_id

    def rebuild_archive(self, job_id, directory, *, overwrite=False, mt_paid=None):
        from .archives import read_archive
        from .mt_pool import configured_route
        from .reports import ReportWriter
        from .common import PROCESSING_VERSION, atomic_bytes, validate_workspace_id
        if directory is None:
            raise ToolError('旧任务重建须用 --output-dir 指定新输出目录；原归档保留。', 'output_path_required')
        task, files = read_archive(self.config, job_id)
        old = task['job']['manifest']
        key = self.config.get_key()
        manifest = {'region': 'beijing', 'access_mode': 'workspace',
                    'workspace_id': validate_workspace_id(self.config.settings.workspace_id),
                    'account': hashlib.sha256(key.encode()).hexdigest(), 'processing_version': PROCESSING_VERSION,
                    'subtitle_version': SUBTITLE_VERSION, 'display_timing_version': DISPLAY_TIMING_VERSION,
                    'translation_strategy_version': 5, 'execution_snapshot': current_execution_snapshot(),
                    'mt_glossary_schedule': 'prefix-v1', 'contract': asr_contract(),
                    'overwrite': bool(overwrite), 'rebuilt_from_archive': job_id,
                    'subtitle_settings': {k: getattr(self.config.settings, k) for k in
                                         ['zh_line_width', 'other_line_width', 'chinese_first']}}
        route = configured_route(self.config, manifest, paid_enabled=None if mt_paid is None else mt_paid == 'allow')
        manifest['mt_route_snapshot'] = route
        for name in ['fingerprint', 'media', 'track', 'channel']:
            if name in old:
                manifest[name] = old[name]
        manifest['audio_offset_ms'] = segment_offset(old)
        manifest['segments'] = [{k: s[k] for k in ['id', 'offset_ms', 'duration_ms'] if k in s} for s in old['segments']]
        validate_segments(manifest)
        sources, warnings = [], []
        for segment in manifest['segments']:
            check_cancel(self.cancel)
            raw = json.loads(files['raw-' + segment['id'] + '.json'])
            items, notes = normalize(raw, manifest['audio_offset_ms'] + segment['offset_ms'], segment['id'],
                                     duration_ms=segment.get('duration_ms'))
            sources.extend(items)
            warnings.extend(notes)
        sources.sort(key=lambda s: (s['begin_ms'], s['end_ms'], s['sentence_id']))
        self.preserve_review_decisions(sources, task['sources'], task['cues'])
        output = Path(directory).resolve() / Path(task['job']['source']).with_suffix('.srt').name
        ReportWriter(self.config, output).validate()
        self.store.check_republish_output(job_id, output, overwrite, operation='归档重建')
        new_id = uuid.uuid4().hex
        cache = self.config.data / 'cache' / new_id
        cache.mkdir(parents=True)
        try:
            for name, content in files.items():
                if name.startswith('raw-'):
                    atomic_bytes(cache / name, content)
            manifest.update(normalized=True, normalized_count=len(sources), normalized_version=NORMALIZER_VERSION,
                            warnings=warnings)
            check_cancel(self.cancel)
            with self.store.lock, self.store.db:
                now = time.time()
                self.store.db.execute('INSERT INTO jobs(id,source,output,output_key,state,manifest,created,updated) '
                                      'VALUES(?,?,?,?,?,?,?,?)',
                                      (new_id, task['job']['source'], str(output), output_key(output), 'queued',
                                       json.dumps(manifest, ensure_ascii=False), now, now))
                self.store._initialize_subtitles(new_id, sources, [make_fragment(s) for s in sources], manifest)
        except BaseException:
            # A failure may arrive after COMMIT. Keep any committed task's raw files.
            with self.store.lock:
                committed = self.store.db.execute('SELECT 1 FROM jobs WHERE id=?', (new_id,)).fetchone()
            if not committed:
                for name in files:
                    if not name.startswith('raw-'):
                        continue
                    (cache / name).unlink(missing_ok=True)
                cache.rmdir()
            raise
        return new_id

    def retime(self, job_id, *, overwrite=False, output_dir=None):
        """Prepare a local display-only publication from finalized saved subtitles."""
        job = self.store.job(job_id)
        manifest = job["manifest"]
        if unknown_submission(manifest, job["state"]):
            raise ToolError("提交结果未知，不能准备显示校时；请先恢复原任务。", "submission_unknown")
        output = self.store.resolve_output(job, output_dir)
        self.store.check_republish_output(job_id, output, overwrite, operation="校时")
        policy = load_policy(manifest, verify_execution=False)
        settings = policy.settings(self.config.settings)
        self.identity(job_id, manifest, persist=False)
        self.validate_saved_subtitles(job_id, manifest)
        cues = self.store.cues(job_id)
        self.validate_retime_ready(manifest, cues)
        self.verify_source_output(job_id, manifest, output)
        candidate = copy.deepcopy(manifest)
        candidate["display_timing_version"] = DISPLAY_TIMING_VERSION
        content, adjustments = self.render_subtitles(cues, settings)
        intent = prepare_intent(output, content, overwrite)
        check_cancel(self.cancel)
        snapshot = self.config.root / ".build" / "retime" / job_id / (str(time.time_ns()) + ".json")
        self.store.snapshot(job_id, snapshot)
        candidate.pop("reflow", None)
        candidate.pop("backup", None)
        candidate.update(display_timing_adjustments=adjustments, publication_intent=intent,
                         expected_srt_sha256=intent["expected_srt_sha256"], overwrite=bool(overwrite),
                         cue_count=len(cues),
                         retime={"snapshot": str(snapshot.relative_to(self.config.root)),
                                 "prepared_at": time.time(),
                                 "from_display_timing_version": policy.display_timing_version})
        check_cancel(self.cancel)
        inspect_intent(output, intent)
        self.store.commit_retime(job_id, candidate, output)


    @staticmethod
    def validate_retime_ready(manifest, cues):
        if (not cues or not (manifest.get("layout_complete") and manifest.get("layout_planned")) or
                any(c["decision"] not in {"original", "translate"} or
                    c["decision"] == "translate" and not c.get("translated") for c in cues)):
            raise ToolError("仅校时需要完整的最终字幕、译文和排版；请先恢复原任务。", "retime_incomplete")

    @staticmethod
    def preserve_review_decisions(sources, previous_sources, previous_cues):
        previous = {s["sentence_id"]: s for s in previous_sources}
        cues_by_sentence = {}
        for cue in previous_cues:
            sentence_id = cue.get("sentence_id", ":".join(cue["cue_id"].split(":")[:2]))
            cues_by_sentence.setdefault(sentence_id, []).append(cue)
        accounted = set()
        compact = lambda value: re.sub(r"\s", "", value)
        for source in sources:
            sentence_id = source["sentence_id"]
            old = previous.get(sentence_id)
            cues = cues_by_sentence.get(sentence_id, [])
            manual = [c for c in cues if decision_origin(c) == "manual"]
            if old and decision_origin(old) == "manual":
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
            source.update(decision=decision[0], source_lang=decision[1], decision_origin="manual",
                          reason=(old if old and decision_origin(old) == "manual" else manual[0])["reason"])
            accounted.add(sentence_id)
        expected = {s["sentence_id"] for s in previous_sources if decision_origin(s) == "manual"}
        expected.update(c.get("sentence_id", ":".join(c["cue_id"].split(":")[:2])) for c in previous_cues
                        if decision_origin(c) == "manual")
        if not expected.issubset(accounted):
            raise ToolError("部分人工语种决定缺少对应完整句，原任务已保留。", "reflow_review")

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
        load_policy(manifest)
        changes = {k: v for k, v in {"track": track, "channel": channel}.items()
                   if v is not None and manifest.get(k) != v}
        if not changes:
            return
        cache = self.config.data / "cache" / job_id
        if (unknown_submission(manifest, job["state"]) or manifest.get("normalized") or any(s.get("task_id") or s.get("submitting") or s.get("raw")
                for s in manifest["segments"]) or (cache.exists() and any(cache.glob("raw-*.json")))):
            raise ToolError("已提交或提交结果未知的任务不能改变音轨／声道；请保留原任务并新建任务。", "input_committed")
        manifest.update(changes)
        manifest["segments"] = []
        for key in ["media_plan", "audio_offset_ms", "upload_limit_bytes"]:
            manifest.pop(key, None)
        self.cleanup_audio(cache)
        self.store.replace_cues(job_id, [])
        self.store.update(job_id, manifest=manifest)

    def prepare_audio(self, media, source, cache, manifest, limit, force_method=None):
        return media.prepare(source, cache, manifest["media"], manifest["track"], manifest["channel"],
                             limit, force_method=force_method)

    def rebuild_audio(self, media, source, cache, manifest):
        original = manifest.get("media_plan")
        with tempfile.TemporaryDirectory(prefix=".rebuild-", dir=cache) as directory:
            prepared = Path(directory)
            plan, rebuilt = self.prepare_audio(media, source, prepared, manifest,
                    manifest["upload_limit_bytes"], original["method"] if original else None)
            self.validate_rebuilt(original, manifest["segments"], plan, rebuilt)
            check_cancel(self.cancel)
            for segment in rebuilt:
                os.replace(prepared / segment["audio"], cache / segment["audio"])

    @staticmethod
    def validate_rebuilt(original, segments, plan, rebuilt):
        fields = ("id", "audio", "offset_ms", "duration_ms", "start_frame", "sample_count", "sample_rate", "codec", "container", "bytes", "sha256")
        matches = all(all(old.get(k) == new.get(k) for k in fields if k in old)
                      for old, new in zip(segments, rebuilt))
        plan_matches = (not original or {k: v for k, v in plan.items() if k != 'reason'} ==
                        {k: v for k, v in original.items() if k != 'reason'})
        if len(rebuilt) != len(segments) or not matches or not plan_matches:
            raise ToolError("恢复提取的编码、分段或时间轴与原记录不同，不能改变已有识别输入。", "cache_mismatch")

    def run(self, job_id):
        self.mt_control.begin()
        self.run_id = uuid.uuid4().hex
        job = self.store.job(job_id)
        if job['state'] == 'archived' or job['manifest'].get('archived'):
            raise ToolError('旧任务已归档，请导出字幕或从转写重建。', 'job_archived')
        if job["state"] in {"completed", "skipped", "cancelled"}:
            return job["state"]
        if unknown_submission(job["manifest"], job["state"]):
            self.store.update(job_id, "submission_unknown", code="submission_unknown")
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

        def client():
            nonlocal api
            if api is None:
                api = Bailian(key, manifest["region"], self.cancel,
                    count=lambda name: self.store.count(job_id, name),
                    progress=lambda name, done, total: self.progress(job_id, name, done, total),
                    workspace_id=manifest["workspace_id"], access_mode=manifest["access_mode"])
            return api

        def save_failure(state, error, code, request_id=""):
            nonlocal handling_failure
            handling_failure = True
            # A late interrupt/cleanup error cannot undo a committed success.
            # Do not reload the shared manifest here: it may contain a newly received task_id.
            if self.store.job(job_id)["state"] != "completed":
                if unknown_submission(manifest, current_stage):
                    state, code = "submission_unknown", "submission_unknown"
                self.store.update(job_id, state, manifest, error, code, request_id)

        try:
            check_cancel(self.cancel)
            execution_policy = load_policy(manifest)
            from .mt_pipeline import validate_provenance
            validate_provenance(self, job_id, manifest, verify_credentials=False)
            output = self.store.ensure_output(job_id)
            published = False
            if "publication_intent" in manifest:
                key, scope = self.identity(job_id, manifest, persist=False)
                intent = manifest["publication_intent"]
                published = inspect_intent(output, intent)
                if manifest.get("expected_srt_sha256") != intent["expected_srt_sha256"]:
                    raise mismatch()
                if published:
                    manifest["backup"] = intent["backup"] if intent["backup"] and Path(intent["backup"]).is_file() else ""
            if published:
                stage("completed")
                if not (manifest.get("reflow") or manifest.get("retime")):
                    self.cleanup_completed_audio(job_id, manifest, cache)
                return "completed"
            if output.exists() and not manifest["overwrite"]:
                stage("skipped")
                return "skipped"
            cache.mkdir(parents=True, exist_ok=True)
            settings = execution_policy.settings(self.config.settings)
            if manifest.get("retime") and not manifest.get("normalized"):
                raise mismatch()
            if manifest.get("reflow") and not manifest.get("normalized"):
                raise ToolError("重排的本地来源／恢复标记不完整。", "cache_mismatch")
            manifest["contract"] = asr_contract()
            # Even normalized/translated cache hits must validate the current account and saved endpoint.
            key, scope = self.identity(job_id, manifest)
            self.mt_runtime_active = not bool(manifest.get('retime'))
            if manifest.get("normalized"):
                self.verify_source_output(job_id, manifest, output)
                self.prepare_output(output)
                return self.finish_subtitles(job_id, manifest, settings, scope, stage, output, cache)
            saved_asr = False
            if self.store.sources(job_id) or self.store.cues(job_id):
                raise mismatch()
            if manifest["segments"]:
                validate_segments(manifest)
                saved_asr = all(saved_raw_path(cache, s).is_file() or s.get("task_id") for s in manifest["segments"])
                # Validate already saved raw before any missing known task is queried.
                for segment in manifest["segments"]:
                    if saved_raw_path(cache, segment).is_file():
                        normalize_raw(cache, manifest, segment)
                    elif segment.get("raw") and not segment.get("task_id"):
                        raise mismatch()
            self.prepare_output(output)
            media = Media(self.config.root, self.cancel)
            if saved_asr:
                audio_offset = segment_offset(manifest)
                self.verify_source_output(job_id, manifest, output)
            else:
                stage("probing")
                if not source.is_file():
                    raise ToolError("源文件已移动或删除；仍需提交识别，必须恢复原媒体。", "input")
                current = fingerprint(source, self.cancel)
                if manifest.get("fingerprint") and manifest["fingerprint"] != current:
                    raise ToolError("源文件内容或时间发生变化；仍需识别的任务不能改变原输入。", "source_changed")
                manifest["fingerprint"] = current
                if not manifest.get("media"):
                    manifest["media"] = media.probe(source)
                track, audio_offset = media.select(manifest["media"], manifest.get("track"),
                                                   manifest.get("channel", settings.channel))
                manifest["track"], manifest["audio_offset_ms"] = track, audio_offset
                if manifest.get("media_plan"):
                    manifest["audio_offset_ms"] = manifest["media_plan"]["audio_offset_ms"]
                manifest.setdefault("channel", settings.channel)
            self.store.update(job_id, manifest=manifest)
            if not manifest["segments"]:
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
            validate_segments(manifest)
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
                    if oss_url and (not isinstance(oss_url, str) or not oss_url.startswith("oss://") or any(c.isspace() for c in oss_url)):
                        raise ToolError("缓存上传引用格式无效。", "cache_mismatch")
                    if not oss_url:
                        if (not audio.exists() or (segment.get("sha256") and
                                media.audio_identity(audio) != {"bytes": segment["bytes"], "sha256": segment["sha256"]})):
                            stage("extracting")
                            self.rebuild_audio(media, source, cache, manifest)
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
            stage("segmenting")
            candidate = copy.deepcopy(manifest)
            cues = []
            for segment in manifest["segments"]:
                raw = json.loads((cache / ("raw-" + segment["id"] + ".json")).read_text(encoding="utf-8"))
                items, warnings = normalize(raw, audio_offset + segment["offset_ms"], segment["id"],
                                            duration_ms=segment.get("duration_ms"))
                cues.extend(items)
                candidate["warnings"].extend(warnings)
            cues.sort(key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))
            candidate.update(normalized=True, normalized_count=len(cues),
                             normalized_version=NORMALIZER_VERSION)
            sources, fragments = cues, [make_fragment(s) for s in cues]
            self.commit_local(job_id, manifest, candidate, self.store.initialize_subtitles, sources, fragments)
            return self.finish_subtitles(job_id, manifest, settings, scope, stage, output, cache)
        except LocalStateUnavailable:
            # Never let a generic handler persist an unconfirmed in-memory state.
            raise
        except (Cancelled, KeyboardInterrupt) as e:
            self.cancel.set()
            e = Cancelled()
            ambiguous = unknown_submission(manifest, current_stage)
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
            self.mt_runtime_active = False
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

    def validate_saved_subtitles(self, job_id, manifest):
        if not manifest.get("normalized"):
            raise mismatch()
        validate_normalized(manifest, self.store.sources(job_id), self.store.cues(job_id))
        from .mt_pipeline import validate_provenance
        validate_provenance(self, job_id, manifest)

    def verify_source_output(self, job_id, manifest, output):
        source = Path(self.store.job(job_id)["source"])
        if not source.is_file():
            return
        if output.exists() and output.samefile(source):
            raise ToolError("目标 SRT 与源媒体是同一文件，请使用其他输出目录。", "output_conflict")
        if output_key(output.parent) == output_key(source.parent):
            if not manifest.get("fingerprint") or manifest["fingerprint"] != fingerprint(source, self.cancel):
                raise ToolError("原路径媒体已变化；请使用其他目录的 --output-dir 保存原任务字幕。", "source_changed")

    def finish_subtitles(self, job_id, manifest, settings, scope, stage, output, cache):
        self.validate_saved_subtitles(job_id, manifest)
        cues = self.store.cues(job_id)
        if manifest.get("retime"):
            self.validate_retime_ready(manifest, cues)
        if not cues:
            manifest["no_speech"] = True
            stage("completed")
            if not (manifest.get("reflow") or manifest.get("retime")):
                self.cleanup_completed_audio(job_id, manifest, cache)
            return "completed"
        if any(c["decision"] == "uncertain" for c in cues):
            raise NeedsReview("部分字幕语种不确定，请用 --review 逐条确认原文或翻译。", "language_uncertain")
        if "publication_intent" not in manifest and not manifest.get("retime"):
            from .mt_pipeline import process_subtitles
            process_subtitles(self, job_id, manifest, settings, scope, stage)
        cues = self.store.cues(job_id)
        check_cancel(self.cancel)
        stage("writing")
        content, adjustments = self.render_subtitles(cues, settings)
        if "publication_intent" not in manifest:
            candidate = copy.deepcopy(manifest)
            intent = prepare_intent(output, content, manifest["overwrite"])
            candidate.update(publication_intent=intent, expected_srt_sha256=intent["expected_srt_sha256"],
                             cue_count=len(cues))
            candidate["display_timing_adjustments"] = adjustments
            self.commit_local(job_id, manifest, candidate,
                              lambda jid, value: self.store.update(jid, manifest=value))
        elif manifest.get("display_timing_adjustments") != adjustments:
            raise mismatch()
        self.verify_source_output(job_id, manifest, output)
        manifest["backup"] = write_srt(output, content, manifest["overwrite"], intent=manifest["publication_intent"])
        stage("completed")
        if not (manifest.get("reflow") or manifest.get("retime")):
            self.cleanup_completed_audio(job_id, manifest, cache)
        return "completed"

    @staticmethod
    def render_subtitles(cues, settings):
        intervals, adjustments = plan_display_timing(cues)
        return render_srt(cues, settings, display_intervals=intervals), adjustments


    def _translate_pending(self, job_id, tasks, *, done, total):
        """Stop new work on failure or cancellation; save in-flight results before returning."""
        errors = []
        workers = self.config.settings.mt_workers
        with ThreadPoolExecutor(max_workers=workers) as executor:
            remaining = iter(tasks)
            futures = {}
            def schedule():
                while (len(futures) < workers and not errors and not self.cancel.is_set()
                       and not self.mt_control.stopping):
                    item = next(remaining, None)
                    if item is None:
                        break
                    members, run = item
                    futures[executor.submit(run)] = members
            try:
                schedule()
                while futures:
                    self.mt_control.stopping  # Observe external cancellation even with silent requests.
                    completed, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                    for future in completed:
                        members = futures.pop(future)
                        try:
                            future.result()
                            done += sum(not c['translated'] for c in members)
                        except ToolError as e:
                            if e.code != 'cancelled':
                                self.mt_control.stop(e)
                            errors.append(e)
                        self.progress(job_id, 'subtitle_translating', done, total)
                    schedule()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, Cancelled)):
                    self.request_cancel(force=True)
                else:
                    self.mt_control.stop(ToolError('翻译阶段本地处理失败，进度已保存。', 'local_io'), force=True)
                raise
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
        candidates = [s for s in manifest["segments"] if s.get("submitting") and not s.get("task_id")]
        if len(candidates) != 1:
            raise ToolError('未知提交必须对应唯一分段；请检查任务记录。', 'submission_unknown')
        segment = candidates[0]
        if task_id:
            if not task_id.isascii() or not all(c.isalnum() or c in "-_" for c in task_id) or len(task_id) > 200:
                raise ToolError("task_id 格式无效。", "input")
            segment["task_id"] = task_id
        elif not allow_resubmit:
            raise ToolError("重新提交需要明确确认可能重复计费。", "submission_unknown")
        else:
            for name in ["oss_url", "upload_expires", "upload_identity"]:
                segment.pop(name, None)
        segment["submitting"] = False
        self.store.update(job_id, "submission_unknown" if unknown_submission(manifest, "queued") else "queued", manifest)

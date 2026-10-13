from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path

from .api import Bailian, upload_limit
from .common import (ASR_MODEL, MT_MODEL, NORMALIZER_VERSION, PROCESSING_VERSION, Cancelled, NeedsReview,
                     SubmissionUnknown, ToolError, atomic_json, check_cancel, connection_identity, fingerprint)
from .config import Config, decrypt_legacy
from .media import Media
from .store import Store
from .subtitles import mt_cache_key, normalize, render_srt, write_srt


class Engine:
    def __init__(self, config: Config, store: Store, cancel=None, progress=lambda *args: None, api_factory=Bailian):
        self.config, self.store = config, store
        self.cancel = cancel or threading.Event()
        self.progress, self.api_factory = progress, api_factory

    def identity(self, job_id, manifest):
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
        if plan and (plan.get("version") != version or plan.get("track") != manifest.get("track") or
                     plan.get("channel") != manifest.get("channel")):
            raise ToolError("媒体缓存的版本／音轨／声道与任务记录不一致。", "cache_mismatch")
        manifest.update(identity)
        manifest["account"], manifest["cache_identity"] = account, scope
        self.store.update(job_id, manifest=manifest)
        return key, scope

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

    def prepare_audio(self, media, source, cache, manifest, limit, force_method=None):
        if manifest.get("processing_version", 1) == 1:
            full = cache / "audio.wav"
            media.extract(source, full, manifest["track"], manifest["channel"])
            segments = media.split(full, cache, limit)
            if len(segments) > 1:
                full.unlink(missing_ok=True)
            return None, segments
        return media.prepare(source, cache, manifest["media"], manifest["track"], manifest["channel"],
                             limit, force_method=force_method)

    def rebuild_audio(self, media, source, cache, manifest):
        original = manifest.get("media_plan")
        plan, rebuilt = self.prepare_audio(media, source, cache, manifest,
                manifest["upload_limit_bytes"], original["method"] if original else None)
        fields = ("id", "audio", "offset_ms", "duration_ms", "start_frame", "sample_count", "sample_rate", "codec", "container", "bytes", "sha256")
        matches = all(all(old.get(k) == new.get(k) for k in fields if k in old)
                      for old, new in zip(manifest["segments"], rebuilt))
        plan_matches = (not original or {k: v for k, v in plan.items() if k != "reason"} ==
                        {k: v for k, v in original.items() if k != "reason"})
        if len(rebuilt) != len(manifest["segments"]) or not matches or not plan_matches:
            raise ToolError("恢复提取的编码、分段或时间轴与原记录不同，不能改变已有识别输入。", "cache_mismatch")

    def run(self, job_id):
        job = self.store.job(job_id)
        if job["state"] in {"completed", "skipped", "cancelled"}:
            return job["state"]
        if job["state"] == "submission_unknown":
            return "submission_unknown"
        manifest = job["manifest"]
        cache = self.config.data / "cache" / job_id
        cache.mkdir(parents=True, exist_ok=True)
        source, output = Path(job["source"]), Path(job["output"])
        settings = self.config.settings
        api, key, scope = None, None, None

        def stage(name, done=None, total=None):
            self.store.update(job_id, name, manifest)
            self.progress(job_id, name, done, total)

        def client():
            nonlocal api
            if api is None:
                api = self.api_factory(key, manifest["region"], self.cancel,
                    count=lambda name: self.store.count(job_id, name),
                    progress=lambda name, done, total: self.progress(job_id, name, done, total),
                    workspace_id=manifest["workspace_id"], access_mode=manifest["access_mode"])
            return api

        try:
            check_cancel(self.cancel)
            if output.exists() and not manifest["overwrite"]:
                if manifest.get("expected_srt_sha256") == hashlib.sha256(output.read_bytes()).hexdigest():
                    stage("completed")
                    return "completed"
                stage("skipped")
                return "skipped"
            stage("probing")
            if not source.is_file():
                raise ToolError("源文件已移动或删除；请重新选择文件。", "input")
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd, test_name = tempfile.mkstemp(prefix=".subtitle-write-test-", dir=output.parent)
                os.close(fd)
                Path(test_name).unlink()
            except OSError as e:
                raise ToolError("视频输出目录不可写；请用 --output-dir 明确选择可写位置。", "output_readonly") from e
            current = fingerprint(source, self.cancel)
            if manifest.get("fingerprint") and manifest["fingerprint"] != current:
                raise ToolError("源文件内容或时间发生变化；缓存未复用。请停止此任务后重新入队。", "source_changed")
            manifest["fingerprint"] = current
            contract = {"asr": ASR_MODEL, "mt": MT_MODEL, "normalizer": NORMALIZER_VERSION,
                        "parameters": {"channel_id": [0], "enable_words": True, "enable_itn": False}}
            if manifest.get("contract") and manifest["contract"] != contract:
                saved = manifest["contract"]
                compatible = (saved.get("normalizer") == 1 and
                              {k: v for k, v in saved.items() if k != "normalizer"} ==
                              {k: v for k, v in contract.items() if k != "normalizer"})
                if not compatible:
                    raise ToolError("缓存接口／断句版本不受支持；请检查任务版本。", "cache_version")
                if manifest.get("normalized"):
                    # Already valid v1 cues and their successful translations stay usable.
                    contract = saved
                else:
                    manifest["normalization_upgraded_from"] = 1
            manifest["contract"] = contract
            # Even normalized/translated cache hits must validate the current account and saved endpoint.
            key, scope = self.identity(job_id, manifest)
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
                    except SubmissionUnknown:
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
                cues = []
                for segment in manifest["segments"]:
                    raw = json.loads((cache / ("raw-" + segment["id"] + ".json")).read_text(encoding="utf-8"))
                    items, warnings = normalize(raw, audio_offset + segment["offset_ms"], segment["id"],
                                                duration_ms=segment.get("duration_ms"))
                    cues.extend(items)
                    manifest["warnings"].extend(warnings)
                cues.sort(key=lambda c: (c["begin_ms"], c["end_ms"], c["cue_id"]))
                self.store.replace_cues(job_id, cues)
                normalized_file = f"normalized-v{NORMALIZER_VERSION}.json"
                atomic_json(cache / normalized_file, cues)
                manifest["normalized"] = True
                manifest["normalized_version"] = NORMALIZER_VERSION
                manifest["normalized_file"] = normalized_file
                self.store.update(job_id, manifest=manifest)
            cues = self.store.cues(job_id)
            if not cues:
                manifest["no_speech"] = True
                stage("completed")
                self.cleanup_audio(cache)
                return "completed"
            if any(c["decision"] == "uncertain" for c in cues):
                raise NeedsReview("部分字幕语种不确定，请用 --review 逐条确认原文或翻译。", "language_uncertain")
            stage("translating")
            self.translate(job_id, cues, scope, client)
            cues = self.store.cues(job_id)
            check_cancel(self.cancel)
            stage("writing")
            content = render_srt(cues, settings)
            manifest["expected_srt_sha256"] = hashlib.sha256(content).hexdigest()
            self.store.update(job_id, manifest=manifest)
            manifest["backup"] = write_srt(output, content, manifest["overwrite"])
            manifest["cue_count"] = len(cues)
            manifest["output_settings"] = asdict(settings)
            stage("completed")
            self.cleanup_audio(cache)
            return "completed"
        except (Cancelled, KeyboardInterrupt) as e:
            self.cancel.set()
            e = Cancelled()
            ambiguous = any(s.get("submitting") and not s.get("task_id") for s in manifest["segments"])
            self.store.update(job_id, "submission_unknown" if ambiguous else "cancelled", manifest,
                              str(e), "submission_unknown" if ambiguous else e.code)
        except SubmissionUnknown as e:
            self.store.update(job_id, "submission_unknown", manifest, str(e), e.code, e.request_id)
        except NeedsReview as e:
            self.store.update(job_id, "needs_review", manifest, str(e), e.code, e.request_id)
        except ToolError as e:
            self.store.update(job_id, "skipped" if e.code == "exists" else "failed", manifest, str(e), e.code, e.request_id)
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Exception text may contain transcripts or credentials; only class is reported.
            self.store.update(job_id, "failed", manifest,
                              "本地数据／文件操作失败（" + type(e).__name__ + "）。请检查磁盘空间、权限或缓存结构。", "local_io")
        state = self.store.job(job_id)["state"]
        self.progress(job_id, state, None, None)
        return state

    def translate(self, job_id, cues, scope, client):
        pending, done = [], 0
        for cue in cues:
            if cue["decision"] != "translate" or cue["translated"]:
                done += 1
                continue
            key = mt_cache_key(cue["text"], cue["source_lang"], **scope)
            cached = self.store.cache_get(key)
            if cached:
                self.store.cue_update(job_id, cue["cue_id"], translated=cached, error="")
                done += 1
            else:
                pending.append((cue, key))
        self.progress(job_id, "translating", done, len(cues))
        if not pending:
            return
        cloud = client()
        errors = []
        def translate_one(cue, key):
            check_cancel(self.cancel)
            self.store.cue_update(job_id, cue["cue_id"], attempts=cue["attempts"] + 1, error="")
            text, request_id = cloud.translate(cue["text"], cue["source_lang"])
            self.store.cache_put(key, text)
            self.store.cue_update(job_id, cue["cue_id"], translated=text, error="")
            return request_id
        workers = self.config.settings.mt_workers
        with ThreadPoolExecutor(max_workers=workers) as executor:
            remaining = iter(pending)
            futures = {}
            def schedule():
                while len(futures) < workers and not errors and not self.cancel.is_set():
                    item = next(remaining, None)
                    if item is None:
                        break
                    cue, key = item
                    futures[executor.submit(translate_one, cue, key)] = cue
            schedule()
            while futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    cue = futures.pop(future)
                    try:
                        future.result()
                        done += 1
                    except ToolError as e:
                        self.store.cue_update(job_id, cue["cue_id"], error=str(e))
                        errors.append(e)
                    except CancelledError:
                        errors.append(Cancelled())
                    self.progress(job_id, "translating", done, len(cues))
                # Stop scheduling after a failed request. Keep the at-most-workers in-flight successes.
                schedule()
        check_cancel(self.cancel)
        if errors:
            raise errors[0]

    @staticmethod
    def cleanup_audio(cache):
        for suffix in ["wav", "flac", "m4a", "mp3", "ogg"]:
            for path in cache.glob("audio*." + suffix):
                path.unlink(missing_ok=True)

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

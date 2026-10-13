from __future__ import annotations

import random
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from opencc import OpenCC
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from .common import (API_ROOTS, ASR_MODEL, MAX_UPLOAD_BYTES, MT_MODEL, SubmissionUnknown,
                     ToolError, check_cancel, connection_identity)


def upload_limit(policy):
    try:
        limit = min(MAX_UPLOAD_BYTES, int(float(policy.get("max_file_size_mb", 1000)) * 1_000_000))
    except (ValueError, TypeError, OverflowError) as e:
        raise ToolError("上传凭证容量字段无效。", "response_schema") from e
    if limit <= 0:
        raise ToolError("上传凭证容量字段无效。", "response_schema")
    return limit


class RequestPacer:
    """Smooth MT starts across workers; 1.05s stays below the documented 60 RPM/RPS=1."""
    def __init__(self, interval=1.05, clock=None):
        self.interval = interval
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.not_before = 0.0

    def wait(self, cancel):
        while True:
            check_cancel(cancel)
            with self.lock:
                now = self.clock()
                delay = self.not_before - now
                if delay <= 0:
                    self.not_before = now + self.interval
                    return
            if cancel.wait(delay):
                check_cancel(cancel)

    def cooldown(self, seconds):
        with self.lock:
            self.not_before = max(self.not_before, self.clock() + seconds)


_MT_PACERS = {}
_MT_PACERS_LOCK = threading.Lock()


def mt_pacer(key, region):
    import hashlib
    identity = (hashlib.sha256(key.encode()).hexdigest(), region, MT_MODEL)
    with _MT_PACERS_LOCK:
        return _MT_PACERS.setdefault(identity, RequestPacer())


class Http:
    """No automatic retries of ASR POST. No response bodies or URLs in logs."""
    def __init__(self, cancel: threading.Event, count=lambda name: None, attempts=4, progress=lambda *args: None, pacer=None):
        self.cancel, self.count, self.attempts = cancel, count, attempts
        self.progress = progress
        self.pacer = pacer or RequestPacer()

    def request(self, method, url, *, name="http", headers=None, retry=True, **kwargs):
        for attempt in range(self.attempts if retry else 1):
            check_cancel(self.cancel)
            if name == "mt":
                self.pacer.wait(self.cancel)
            self.count(name)
            try:
                response = requests.request(method, url, headers=headers, timeout=(15, 90),
                                            allow_redirects=False, **kwargs)
            except requests.RequestException as e:
                if not retry:
                    raise SubmissionUnknown() from e
                if attempt + 1 == self.attempts:
                    raise ToolError("网络连接失败或超时；可重试当前阶段。", "network") from e
                self.progress("retry", attempt + 1, self.attempts)
                self.pause(attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if not retry and response.status_code >= 500:
                    response.close()
                    raise SubmissionUnknown()
                if retry and attempt + 1 < self.attempts:
                    retry_after = response.headers.get("Retry-After", "")
                    throttled = name == "mt" and response.status_code == 429
                    response.close()
                    self.progress("retry", attempt + 1, self.attempts)
                    if throttled:
                        try:
                            cooldown = float(retry_after) if retry_after else 15 * 2 ** attempt
                        except ValueError:
                            cooldown = 15 * 2 ** attempt
                        self.pacer.cooldown(min(60, max(1, cooldown)))
                    else:
                        self.pause(attempt, retry_after)
                    continue
            if not 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except (ValueError, requests.RequestException):
                    body = {}
                status = response.status_code
                response.close()
                if not isinstance(body, dict):
                    body = {}
                # Do not echo server messages: they may contain input text / signed URLs.
                error = body.get("error")
                code = str(body.get("code", error.get("code", "http_error") if isinstance(error, dict) else "http_error"))
                request_id = str(body.get("request_id", ""))
                hint = {401: "Key 无效或地域不匹配", 403: "账户、模型授权或文件访问被拒绝",
                        429: "请求限流", 400: "请求参数或文件格式被拒绝", 404: "任务或服务不存在"}.get(
                            status, "服务或网络请求失败")
                raise ToolError(f"{hint}（HTTP {status}，{code}）。", code, request_id)
            return response
        raise ToolError("重试次数已用尽。", "retry_exhausted")

    def pause(self, attempt, retry_after=""):
        try:
            seconds = min(60, max(0, float(retry_after))) if retry_after else min(30, 2 ** attempt)
        except ValueError:
            seconds = min(30, 2 ** attempt)
        if self.cancel.wait(seconds + random.uniform(0, 0.4)):
            check_cancel(self.cancel)

    def json(self, method, url, **kwargs):
        response = self.request(method, url, **kwargs)
        try:
            body = response.json()
        except ValueError as e:
            if kwargs.get("retry") is False:
                raise SubmissionUnknown() from e
            raise ToolError("服务未返回合法 JSON。", "response_schema") from e
        finally:
            response.close()
        if not isinstance(body, dict):
            if kwargs.get("retry") is False:
                raise SubmissionUnknown()
            raise ToolError("服务返回结构不符合文档。", "response_schema")
        if body.get("code") and str(body["code"]) not in {"200", "Success", "SUCCESS"}:
            raise ToolError(f"百炼业务错误：{body['code']}。", body["code"], body.get("request_id", ""))
        return body


class Bailian:
    def __init__(self, key: str, region: str, cancel: threading.Event,
                 count=lambda name: None, progress=lambda *args: None,
                 *, workspace_id="", access_mode="legacy_public"):
        identity = connection_identity({"region": region, "workspace_id": workspace_id, "access_mode": access_mode})
        self.base = (f"https://{identity['workspace_id']}.cn-beijing.maas.aliyuncs.com"
                     if access_mode == "workspace" else API_ROOTS["beijing"])
        self.upload_base = API_ROOTS["beijing"]
        self.headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        self.http = Http(cancel, count, progress=progress, pacer=mt_pacer(key, region))
        self.progress = progress
        self.cc = OpenCC("t2s")

    def policy(self):
        body = self.http.json("GET", self.upload_base + "/api/v1/uploads",
                              name="upload_policy", headers=self.headers,
                              params={"action": "getPolicy", "model": ASR_MODEL})
        policy = body.get("data", {})
        required = {"policy", "signature", "upload_dir", "upload_host", "oss_access_key_id",
                    "x_oss_object_acl", "x_oss_forbid_overwrite"}
        if not isinstance(policy, dict) or not required.issubset(policy):
            raise ToolError("上传凭证缺少官方表单字段。", "response_schema", body.get("request_id", ""))
        host = urlsplit(policy["upload_host"])
        if host.scheme != "https" or not host.hostname or host.username:
            raise ToolError("上传凭证的地址必须为 HTTPS。", "response_schema")
        return policy

    def upload(self, path: Path):
        # Refresh policy & create a new object per upload attempt. Never reuse consumed multipart data.
        for attempt in range(4):
            policy = self.policy()
            limit = upload_limit(policy)
            if path.stat().st_size > limit:
                error = ToolError(f"音频超过当前上传凭证限制（{limit // 1_000_000} MB），需要分段。", "upload_size")
                error.limit = limit
                raise error
            filename = uuid.uuid4().hex + path.suffix
            object_key = policy["upload_dir"].rstrip("/") + "/" + filename
            fields = [("OSSAccessKeyId", policy["oss_access_key_id"]), ("policy", policy["policy"]),
                      ("Signature", policy["signature"]), ("key", object_key),
                      ("x-oss-object-acl", policy["x_oss_object_acl"]),
                      ("x-oss-forbid-overwrite", policy["x_oss_forbid_overwrite"]),
                      ("success_action_status", "200")]
            try:
                with path.open("rb") as audio:
                    mime = {".wav": "audio/wav", ".flac": "audio/flac", ".m4a": "audio/mp4",
                            ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}.get(path.suffix.lower())
                    if mime is None:
                        raise ToolError("上传音频容器不受支持。", "media")
                    encoder = MultipartEncoder(fields + [("file", (filename, audio, mime))])
                    last_update = [0.0]
                    self.progress("uploading", 0, encoder.len)
                    def update(monitor):
                        check_cancel(self.http.cancel)
                        if time.monotonic() - last_update[0] >= 0.1 or monitor.bytes_read == monitor.len:
                            self.progress("uploading", monitor.bytes_read, monitor.len)
                            last_update[0] = time.monotonic()
                    monitor = MultipartEncoderMonitor(encoder, update)
                    self.http.count("upload")
                    # No API Key is sent to the returned OSS host.
                    response = requests.post(policy["upload_host"], data=monitor,
                                             headers={"Content-Type": monitor.content_type},
                                             timeout=(15, 120), allow_redirects=False)
                    status = response.status_code
                    response.close()
                    if status in (200, 201, 204):
                        return "oss://" + object_key
                    if status not in {429, 500, 502, 503, 504}:
                        raise ToolError(f"临时音频上传失败（HTTP {status}）。", "upload_failed")
            except requests.RequestException as e:
                if attempt == 3:
                    raise ToolError("临时上传网络失败。", "network") from e
            if attempt == 3:
                raise ToolError("临时上传重试次数已用尽。", "upload_failed")
            self.progress("retry", attempt + 1, 4)
            self.http.pause(attempt)

    def submit(self, oss_url: str):
        body = self.http.json("POST", self.base + "/api/v1/services/audio/asr/transcription",
                              name="asr_submit", retry=False,
                              headers={**self.headers, "X-DashScope-Async": "enable",
                                       "X-DashScope-OssResourceResolve": "enable"},
                              json={"model": ASR_MODEL, "input": {"file_url": oss_url},
                                    "parameters": {"channel_id": [0], "enable_words": True,
                                                   "enable_itn": False}})
        output = body.get("output")
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise SubmissionUnknown(body.get("request_id", ""))
        return task_id, body.get("request_id", "")

    def query(self, task_id: str):
        return self.http.json("GET", self.base + "/api/v1/tasks/" + quote(task_id, safe=""),
                              name="asr_query", headers=self.headers)

    def download(self, url: str):
        parsed = urlsplit(url)
        # Official examples include HTTP OSS result links. Upgrade without altering signed query.
        if parsed.scheme == "http" and parsed.hostname and parsed.hostname.endswith(".aliyuncs.com"):
            url = "https://" + url[len("http://"):]
        elif parsed.scheme != "https":
            raise ToolError("结果下载地址不是受支持的 HTTPS 链接。", "response_schema")
        # Never attach the API Authorization header to result storage.
        return self.http.json("GET", url, name="result_download")

    def translate(self, text: str, source_lang="auto", *, tm_list=None) -> tuple[str, str]:
        options = {"source_lang": source_lang, "target_lang": "Chinese"}
        if tm_list is not None:
            from .subtitles import validate_mt_input
            if (not isinstance(tm_list, list) or any(not isinstance(item, dict) or
                    set(item) != {"source", "target"} or
                    any(not isinstance(item[k], str) or not item[k] for k in ("source", "target")) for item in tm_list)):
                raise ToolError("翻译记忆结构无效。", "mt_context")
            validate_mt_input(text, source_lang, tm_list)
            if tm_list:
                options["tm_list"] = tm_list
        body = self.http.json("POST", self.base + "/compatible-mode/v1/chat/completions",
                              name="mt", headers=self.headers,
                              json={"model": MT_MODEL, "messages": [{"role": "user", "content": text}],
                                    "stream": False, "translation_options": options})
        try:
            choice = body["choices"][0]
            translated = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(translated, str):
                raise ValueError()
            translated = translated.strip()
            if not translated or "-->" in translated or "```" in translated or "\x00" in translated:
                raise ValueError()
            if len(translated) > max(500, len(text) * 12):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ToolError("译文为空、截断或格式异常；该条保留待重试。", "mt_invalid", body.get("id", "")) from e
        return self.cc.convert(translated), body.get("id", "")

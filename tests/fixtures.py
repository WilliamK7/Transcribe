from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

from subtitle_tool.api import Http
from subtitle_tool.config import Config
from subtitle_tool.engine import Engine
from subtitle_tool.media import Media, run_process
from subtitle_tool.store import Store

ROOT = Path(__file__).resolve().parent.parent
TEMP = ROOT / ".build" / "test-temp"
TEMP.mkdir(parents=True, exist_ok=True)


def result(*sentences):
    return {"file_url": "oss://test/audio.wav", "audio_info": {"format": "wav", "sample_rate": 16000},
            "transcripts": [{"channel_id": 0, "sentences": [
                {"begin_time": i * 1000, "end_time": i * 1000 + 900, "text": text, "language": lang}
                for i, (text, lang) in enumerate(sentences)]}]}


def response(body, status=200, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode()
    resp._content_consumed = True
    resp.headers.update(headers or {})
    return resp


def timed_raw(tokens, *, word_ms=200, language="en", pauses=None, start=0):
    words, at = [], start
    for index, token in enumerate(tokens):
        at += (pauses or {}).get(index, 0)
        words.append({"begin_time": at, "end_time": at + word_ms, "text": token, "punctuation": ""})
        at += word_ms
    return {"transcripts": [{"channel_id": 0, "sentences": [{
        "begin_time": start, "end_time": at, "text": ("" if language in {"zh", "yue", "ja"} else " ").join(tokens),
        "language": language, "words": words}]}]}


class FakeService:
    """Offline service with injectable HTTP failures and out-of-order MT completion."""
    def __init__(self, raw):
        self.raw, self.calls, self.uploads, self.submit_error = raw, [], [], None
        self.mt_failure = set()
        self.query_failure = False
        self.download_failure = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/uploads"):
            return response({"request_id": "offline-policy", "data": {
                "policy": "offline", "signature": "offline", "upload_dir": "offline/dir",
                "upload_host": "https://offline-upload.oss-cn-beijing.aliyuncs.com",
                "expire_in_seconds": 300, "max_file_size_mb": 1000,
                "oss_access_key_id": "offline", "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true"}})
        if url.endswith("/asr/transcription"):
            if self.submit_error:
                raise self.submit_error
            return response({"request_id": "offline-submit", "output": {"task_id": "offline-task", "task_status": "PENDING"}})
        if "/tasks/" in url:
            if self.query_failure:
                raise requests.ConnectionError()
            return response({"request_id": "offline-query", "output": {"task_status": "SUCCEEDED", "result": {
                "transcription_url": "https://offline-result.oss-cn-beijing.aliyuncs.com/raw.json?Signature=offline"}}})
        if "offline-result" in url:
            if self.download_failure:
                raise requests.ConnectionError()
            return response(self.raw)
        if url.endswith("/chat/completions"):
            source = kwargs["json"]["messages"][0]["content"]
            if source in self.mt_failure:
                return response({"code": "Unauthorized", "request_id": "offline-denied"}, 403)
            if source.startswith("First"):
                time.sleep(0.06)
            return response({"id": "offline-mt", "choices": [{"finish_reason": "stop", "message": {
                "content": "繁體譯文：" + source}}]})
        raise AssertionError("Unexpected request " + url)

    def upload(self, url, **kwargs):
        monitor = kwargs["data"]
        body = b""
        while chunk := monitor.read(4096):
            body += chunk
        self.uploads.append((url, kwargs["headers"], body))
        return response({})

    def named(self, suffix):
        return [c for c in self.calls if c[1].endswith(suffix)]


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        os.environ.pop("DASHSCOPE_API_KEY", None)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()
        self.environment.stop()


def point_sentence(at, text="Yes.", language="en"):
    return {"begin_time": at, "end_time": at, "text": text, "language": language,
            "words": [{"begin_time": at, "end_time": at, "text": text[:-1], "punctuation": text[-1]}]}


@unittest.skipUnless(os.name == "nt" and (ROOT / "tools" / "ffmpeg.exe").exists(), "Windows/bundled tools required")
class PipelineFixture(TemporaryTest):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory(dir=TEMP)
        cls.fixture = Path(cls.fixture_dir.name) / "sample.mkv"
        run_process([ROOT / "tools" / "ffmpeg.exe", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=10:d=5",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=16000", "-c:v", "ffv1", "-c:a", "pcm_s16le", cls.fixture], threading.Event())

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        super().setUp()
        self.media_tools = mock.patch.object(Media, "executable", lambda media, name: ROOT / "tools" / (name + ".exe"))
        self.media_tools.start()
        self.config = Config(self.path)
        self.config.settings.workspace_id = "offline-space"
        self.config.set_key("offline-test-key-no-cloud-authorization")
        self.source = self.path / "中文 多点.片段 & (1).mkv"
        shutil.copyfile(self.fixture, self.source)
        self.store = Store(self.config.data / "jobs.sqlite")
        self.job_id = self.store.enqueue(self.source, "beijing", workspace_id="offline-space")

    def tearDown(self):
        self.store.close()
        self.media_tools.stop()
        super().tearDown()

    def run_fake(self, service):
        with mock.patch("requests.request", side_effect=service.request), mock.patch("requests.post", side_effect=service.upload), mock.patch.object(Http, "pause"):
            return Engine(self.config, self.store).run(self.job_id)

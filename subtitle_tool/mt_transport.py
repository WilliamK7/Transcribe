"""MT requests isolated in a process with a complete-response deadline."""
from __future__ import annotations

import base64
import json as jsonlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .common import ToolError


MAX_RESPONSE = 4 * 1024 * 1024


@dataclass(frozen=True)
class HTTPReply:
    status: int
    body: bytes
    retry_after: str = ''


class TransportError(ToolError):
    def __init__(self, category):
        if category not in {'network', 'deadline', 'transport', 'response_too_large'}:
            category = 'transport'
        self.category = category
        self.retryable = category in {'network', 'deadline'}
        self.status = None
        super().__init__({'network': '翻译网络连接或读取失败。', 'deadline': '翻译请求超过总时限。',
                          'transport': '翻译网络子进程异常，进度已保存。',
                          'response_too_large': '翻译响应超过 4 MiB 上限，进度已保存。'}[category],
                         'mt_request_deadline' if category == 'deadline' else 'mt_' + category)


class ProcessTransport:
    def request(self, method, url, *, headers, json=None, control, timeout=120.0, drain=True):
        control.check_start()
        deadline = time.monotonic() + timeout
        payload = jsonlib.dumps({'method': method, 'url': url, 'headers': headers, 'json': json},
                                ensure_ascii=False).encode('utf-8')
        # Do not pass account credentials via inherited environment variables either.
        environment = {k: v for k, v in os.environ.items()
                       if k.upper() not in {'DASHSCOPE_API_KEY', 'OPENAI_API_KEY'}}
        process = None
        try:
            control.check_start()
            process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('_mt_http_worker.py'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, close_fds=True, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            first = True
            while True:
                control.check_inflight(drain=drain)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError('deadline')
                try:
                    stdout, _ = process.communicate(payload if first else None, timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    first = False
            control.check_inflight(drain=drain)
            if time.monotonic() >= deadline:
                raise TransportError('deadline')
            if process.returncode != 0 or len(stdout) > MAX_RESPONSE * 4 // 3 + 4096:
                raise TransportError('transport')
            try:
                reply = jsonlib.loads(stdout)
                if not isinstance(reply, dict):
                    raise ValueError()
                if 'error' in reply:
                    raise TransportError(reply['error'])
                body = base64.b64decode(reply['body'], validate=True)
                if len(body) > MAX_RESPONSE:
                    raise TransportError('response_too_large')
                status, retry_after = reply['status'], reply.get('retry_after', '')
                if type(status) is not int or not 100 <= status <= 599 or not isinstance(retry_after, str):
                    raise ValueError()
                return HTTPReply(status, body, retry_after[:128])
            except (ValueError, KeyError, TypeError, RecursionError):
                raise TransportError('transport') from None
        except OSError:
            raise TransportError('transport') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                # Reap the child and join communicate's pipe readers before returning.
                process.communicate()
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()

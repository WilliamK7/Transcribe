"""Isolated Windows CLI SIGINT exercise; never opens the user's data directory."""
from __future__ import annotations

import _thread
import contextlib
import ctypes
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from subtitle_tool import main as cli
from subtitle_tool.config import Config
from subtitle_tool.mt_transport import ProcessTransport
from tests.test_mt_runtime import ChildProcesses, LocalServer, quota
from tests.test_translation_v3 import batch_response


def main():
    root, job_id, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    server = LocalServer()
    server.body = batch_response(['迟到的完整合法译文']).content
    interrupted = []
    backend_calls = {'primary': 0, 'fallback': 0}
    console_signals = []
    console_mode = mode.startswith('console_')

    class LocalTransport(ProcessTransport):
        def request(self, method, url, **kwargs):
            if method == 'GET':
                return quota(0) if 'bailian' in mode else quota()
            backend_calls['primary' if url.startswith('https://openrouter.ai/') else 'fallback'] += 1
            return super().request(method, server.url + '/hang', **kwargs)

    def interrupt():
        if not server.entered.wait(5):
            return
        interrupted.append(time.monotonic())
        def send():
            if console_mode:
                # Windows launchers may inherit the ignore-Ctrl+C attribute from the harness.
                ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
                success = bool(ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0))
                console_signals.append(success)
                if not success:
                    _thread.interrupt_main()
            else:
                _thread.interrupt_main()
        send()
        if mode.endswith('twice'):
            time.sleep(.2)
            send()

    timer = threading.Thread(target=interrupt)
    timer.start()
    output, errors = io.StringIO(), io.StringIO()
    try:
        with ChildProcesses() as children, mock.patch.object(cli, 'Config', side_effect=lambda: Config(root)), \
                mock.patch('subtitle_tool.mt_runtime.ProcessTransport', LocalTransport), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(['--resume', job_id, '--non-interactive'])
        elapsed = time.monotonic() - interrupted[0] if interrupted else None
        active = children.active_count
        # A distinct process must acquire the lock while this CLI process is still alive.
        probe = subprocess.run([sys.executable, '-c',
            'import sys; from pathlib import Path; from subtitle_tool.instance import ProcessLock; '
            'lock=ProcessLock(Path(sys.argv[1])); ok=lock.acquire(); lock.close(); sys.exit(0 if ok else 1)',
            str(root / 'data')], capture_output=True, timeout=5, close_fds=True)
        print(json.dumps({'exit_code': code, 'drain_seconds': elapsed, 'active_children': active,
                          'lock_reacquired': probe.returncode == 0,
                          'signal_kind': 'windows_console_ctrl_c' if console_mode else 'python_sigint',
                          'console_signals_delivered': console_signals,
                          'backend_calls': backend_calls,
                          'billing_hint': '仍可能计费' in errors.getvalue()}))
    finally:
        timer.join(6)
        server.close()


if __name__ == '__main__':
    main()

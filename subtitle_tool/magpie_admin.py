"""Portable Magpie administration shared by channel and trial maintenance."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .common import ToolError


class MagpieAdmin:
    def __init__(self, config, control, *, job_id='', lock=None):
        self.config, self.control, self.job_id = config, control, job_id
        # Callers hold this reentrant lock across related reads and mutations.
        # Each worker has its own cancellation control, even when sharing a lock.
        self.lock = lock if lock is not None else threading.RLock()

    def registry(self):
        return json.loads((Path(self.config.settings.magpie_data_dir) / 'providers.json').read_bytes())

    def audit(self, reason, model='', before=None, after=None):
        path = self.config.root / '.build' / 'mt-diagnostics' / 'trial-management.jsonl'
        record = {'time': time.time(), 'actor': 'transcribe.trial-manager', 'job_id': self.job_id,
                  'reason': reason, 'model': model, 'before': before, 'after': after}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            return True
        except OSError:
            return False

    def command(self, *args, control=None):
        # A borrowing worker must be able to stop its own command independently.
        control = control if control is not None else self.control
        control.check_start()
        folder = Path(self.config.settings.magpie_data_dir)
        executable = folder.parent / 'magpie.com'
        if folder.name != 'data' or not (folder.parent / '.portable').is_file() or not executable.is_file():
            raise ToolError('无法管理 Magpie 入口；请检查便携目录。', 'magpie_group')
        process = None
        try:
            deadline = time.monotonic() + (30 if args[:2] == ('provider', 'refresh') else 5)
            process = subprocess.Popen([str(executable), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while process.poll() is None:
                control.check_inflight(drain=False)
                if time.monotonic() >= deadline:
                    raise ValueError()
                time.sleep(.05)
            if process.returncode:
                raise ValueError()
            control.check_start()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ToolError('无法更新 Magpie 入口；请检查便携配置和保存记录。', 'magpie_group') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()

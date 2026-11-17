"""Shared trial records and role reservations, without a discovery worker."""
from __future__ import annotations

import json
import threading

from .common import ToolError, atomic_json

TRIAL_PROVIDER = 'transcribe-bailian-trial'


class TrialState:
    def __init__(self, config, *, lock=None):
        self.path = config.data / 'bailian-trial-state.json'
        self.lock = lock if lock is not None else threading.RLock()
        self._data = None

    @property
    def data(self):
        # Load on the first role operation; preserve read failures for the caller.
        with self.lock:
            if self._data is None:
                self._data = self.read()
            return self._data

    def read(self):
        if self.path.exists():
            try:
                value = json.loads(self.path.read_bytes())
            except (OSError, ValueError):
                raise ToolError('百炼试用状态无效，请检查保存记录。', 'mt_config') from None
            if (not isinstance(value, dict) or value.get('version') != 1 or
                    not isinstance(value.get('models'), dict) or not isinstance(value.get('evaluations'), dict) or
                    type(value.get('paused')) is not bool or not isinstance(value.get('excluded'), list) or
                    any(not isinstance(m, str) for m in value['excluded']) or
                    any(not isinstance(r, dict) or not isinstance(m, str) for m, r in value['models'].items())):
                raise ToolError('百炼试用状态无效，请检查保存记录。', 'mt_config')
            return value
        return {'version': 1, 'paused': False, 'excluded': [], 'models': {}, 'evaluations': {}}

    def save(self, control):
        with self.lock:
            control.check_start()
            state = self.data
            if self.path.exists():
                latest = self.read()
                state['paused'] = latest.get('paused', False)
                state['excluded'] = latest.get('excluded', [])
            atomic_json(self.path, state)

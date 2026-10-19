"""Best-effort, content-free MT validation diagnostics for the local machine."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

from .mt import VALIDATION_CATEGORIES


_WRITE_LOCK = threading.Lock()


def safe_request_id(value):
    if isinstance(value, str) and re.fullmatch(
            r'(?:chatcmpl-[0-9a-fA-F]{16,64}|(?:chatcmpl-)?[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})', value):
        return value
    return ''


class MTDiagnostics:
    def __init__(self, root, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid diagnostic job id')
        self.path = root / '.build' / 'mt-diagnostics' / (job_id + '.jsonl')

    def write(self, event):
        try:
            if event.get('kind') not in {'invalid_response', 'split'}:
                return False
            record = {'time': datetime.now(timezone.utc).isoformat(), 'kind': event['kind']}
            if event.get('category') in VALIDATION_CATEGORIES:
                record['category'] = event['category']
            if event.get('stage') in {'reference', 'subtitle'}:
                record['stage'] = event['stage']
            group_key = event.get('group_key')
            if isinstance(group_key, str) and re.fullmatch(r'[0-9a-f]{64}', group_key):
                record['group_key'] = group_key
            for key in ['fragment_start', 'fragment_end', 'depth', 'expected_count', 'actual_count',
                        'begin_ms', 'end_ms']:
                value = event.get(key)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    record[key] = value
            if event['kind'] == 'invalid_response' and event.get('actual_count') is None:
                record['actual_count'] = None
            if type(event.get('repair')) is bool:
                record['repair'] = event['repair']
            record['request_id'] = safe_request_id(event.get('request_id'))
            with _WRITE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open('a', encoding='utf-8', newline='\n') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            return True
        except (OSError, ValueError, TypeError, OverflowError):
            return False

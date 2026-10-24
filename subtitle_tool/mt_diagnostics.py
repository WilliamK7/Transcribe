"""Best-effort, content-free MT validation diagnostics for the local machine."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

from .mt import VALIDATION_CATEGORIES


_WRITE_LOCK = threading.Lock()
_FORMAT_ENUMS = {
    'finish_reason': {'missing', 'null', 'stop', 'length', 'content_filter', 'tool_calls', 'function_call', 'error', 'other'},
    'content_type': {'missing', 'null', 'string', 'object', 'array', 'boolean', 'number', 'other'},
    'json_root_type': {'unavailable', 'invalid', 'null', 'string', 'object', 'array', 'boolean', 'number', 'other'},
}


def _json_type(value):
    if value is None:
        return 'null'
    return {str: 'string', dict: 'object', list: 'array', bool: 'boolean',
            int: 'number', float: 'number'}.get(type(value), 'other')


def batch_response_metadata(body):
    """Inspect formatting without retaining model text, IDs or arbitrary server values."""
    metadata = {'finish_reason': 'missing', 'content_type': 'missing', 'json_root_type': 'unavailable'}
    choices = body.get('choices') if isinstance(body, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return metadata
    choice = choices[0]
    if 'finish_reason' in choice:
        finish = choice['finish_reason']
        metadata['finish_reason'] = ('null' if finish is None else
                                     finish if isinstance(finish, str) and finish in _FORMAT_ENUMS['finish_reason']
                                     else 'other')
    message = choice.get('message')
    if not isinstance(message, dict) or 'content' not in message:
        return metadata
    content = message['content']
    metadata['content_type'] = _json_type(content)
    if isinstance(content, str):
        metadata.update(content_length=len(content), code_fence='```' in content)
        try:
            metadata['json_root_type'] = _json_type(json.loads(content))
        except (ValueError, RecursionError):
            metadata['json_root_type'] = 'invalid'
    return metadata


def safe_request_id(value):
    if isinstance(value, str) and re.fullmatch(
            r'(?:chatcmpl-[0-9a-fA-F]{16,64}|(?:chatcmpl-)?[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})', value):
        return value
    return ''


def safe_model_id(value):
    if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}', value)
            and '://' not in value and not value.lower().startswith(('sk-', 'bearer'))):
        return value
    return ''


class MTDiagnostics:
    def __init__(self, root, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid diagnostic job id')
        self.path = root / '.build' / 'mt-diagnostics' / (job_id + '.jsonl')

    def _append(self, record):
        with _WRITE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return True

    def write_runtime(self, event):
        """Separate callback and whitelist; runtime events never count as format repairs."""
        from .mt_runtime import RUNTIME_CATEGORIES
        try:
            if event.get('kind') not in {'runtime_request', 'runtime_wait', 'runtime_quota', 'runtime_drain',
                                       'runtime_fallback', 'runtime_commit'}:
                return False
            record = {'time': datetime.now(timezone.utc).isoformat(), 'kind': event['kind']}
            if isinstance(event.get('category'), str) and event['category'] in RUNTIME_CATEGORIES:
                record['category'] = event['category']
            self._routing(record, event)
            for field in ['attempt', 'status', 'duration_ms', 'shared_wait_ms', 'pacing_wait_ms',
                          'discarded', 'used', 'limit', 'remaining', 'local_remaining', 'committed_count']:
                value = event.get(field)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    record[field] = value
            for field in ['retryable', 'deadline_terminated', 'cached']:
                if type(event.get(field)) is bool:
                    record[field] = event[field]
            observed = event.get('observed_at')
            if isinstance(observed, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00', observed):
                datetime.fromisoformat(observed)
                record['observed_at'] = observed
            return self._append(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    def write(self, event):
        try:
            if event.get('kind') not in {'invalid_response', 'split'}:
                return False
            record = {'time': datetime.now(timezone.utc).isoformat(), 'kind': event['kind']}
            self._routing(record, event)
            if event.get('category') in VALIDATION_CATEGORIES:
                record['category'] = event['category']
            for key in ['fragment_start', 'fragment_end', 'depth', 'expected_count', 'actual_count',
                        'begin_ms', 'end_ms', 'content_length']:
                value = event.get(key)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    record[key] = value
            if event['kind'] == 'invalid_response' and event.get('actual_count') is None:
                record['actual_count'] = None
            if type(event.get('repair')) is bool:
                record['repair'] = event['repair']
            if type(event.get('code_fence')) is bool:
                record['code_fence'] = event['code_fence']
            for key, allowed in _FORMAT_ENUMS.items():
                value = event.get(key)
                if isinstance(value, str) and value in allowed:
                    record[key] = value
            record['request_id'] = safe_request_id(event.get('request_id'))
            return self._append(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    @staticmethod
    def _routing(record, event):
        for field in ['model', 'actual_model']:
            model = safe_model_id(event.get(field))
            if model:
                record[field] = model
        if type(event.get('request_version')) is int and event['request_version'] in {1, 2, 3}:
            record['request_version'] = event['request_version']
        if event.get('stage') in {'reference', 'subtitle'}:
            record['stage'] = event['stage']
        if event.get('quota_source') in {'local_ledger', 'server_observation'}:
            record['quota_source'] = event['quota_source']
        for field, pattern in [('run_id', r'[0-9a-f]{32}'), ('group_key', r'[0-9a-f]{64}')]:
            value = event.get(field)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                record[field] = value
        for field, allowed in [('backend', {'primary', 'fallback'}), ('provider', {'openrouter', 'bailian', 'openai_compatible'}),
                ('reason', {'quota_exhausted', 'quota_local_empty', 'format_exhausted', 'retry_after', 'transient_exhausted'})]:
            value = event.get(field)
            if isinstance(value, str) and value in allowed:
                record[field] = value

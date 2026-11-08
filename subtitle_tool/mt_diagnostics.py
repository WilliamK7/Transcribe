"""Best-effort, content-free MT validation diagnostics for the local machine."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

from .mt_quality import VALIDATION_CATEGORIES


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

    def write_route(self, event):
        """Write only fixed route classifications and bounded, non-content metadata."""
        from .mt_routing import _CATEGORIES, _SCOPES
        try:
            if event.get('kind') not in {'route_attempt', 'route_failure', 'route_complete', 'route_reply'}:
                return False
            record = {'time': datetime.now(timezone.utc).isoformat(), 'kind': event['kind']}
            self._routing(record, event)
            for field in ['entry']:
                value = safe_model_id(event.get(field))
                if value:
                    record[field] = value
            for field, allowed in [('tier', {'free', 'paid'}), ('scope', _SCOPES),
                                   ('category', _CATEGORIES)]:
                if event.get(field) in allowed:
                    record[field] = event[field]
            for field in ['attempt', 'status', 'duration_ms']:
                value = event.get(field)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    record[field] = value
            if type(event.get('cached')) is bool:
                record['cached'] = event['cached']
            if type(event.get('dispatched')) is bool:
                record['dispatched'] = event['dispatched']
            return self._append(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False


    def write(self, event):
        try:
            if event.get('kind') != 'invalid_response':
                return False
            record = {'time': datetime.now(timezone.utc).isoformat(), 'kind': event['kind']}
            self._routing(record, event)
            if event.get('category') in VALIDATION_CATEGORIES:
                record['category'] = event['category']
            for key in ['expected_count', 'actual_count', 'content_length']:
                value = event.get(key)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    record[key] = value
            if event.get('actual_count') is None:
                record['actual_count'] = None
            if type(event.get('code_fence')) is bool:
                record['code_fence'] = event['code_fence']
            for key, allowed in _FORMAT_ENUMS.items():
                value = event.get(key)
                if isinstance(value, str) and value in allowed:
                    record[key] = value
            return self._append(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    @staticmethod
    def _routing(record, event):
        for field in ['model', 'actual_model']:
            model = safe_model_id(event.get(field))
            if model:
                record[field] = model
        if event.get('stage') in {'glossary', 'draft', 'align', 'review', 'correct'}:
            record['stage'] = event['stage']
        for field, pattern in [('run_id', r'[0-9a-f]{32}'), ('group_key', r'[0-9a-f]{64}'),
                               ('gateway_session', r'transcribe-[0-9a-f]{32}')]:
            value = event.get(field)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                record[field] = value

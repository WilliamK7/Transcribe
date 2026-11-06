"""Validate complete translation mappings returned by a model."""
from __future__ import annotations

import json
import unicodedata

from .common import ToolError


VALIDATION_CATEGORIES = {
    'response_structure': '响应外层结构异常', 'json_invalid': 'JSON 无效',
    'json_duplicate_key': 'JSON 重复键', 'id_missing': '编号缺失',
    'id_out_of_range': '编号越界',
    'translation_invalid': '译文无效', 'unfinished': '非正常结束',
}


class BatchValidationError(ValueError):
    def __init__(self, category, actual_count=None):
        super().__init__(VALIDATION_CATEGORIES[category])
        self.category, self.actual_count = category, actual_count


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey()
        value[key] = item
    return value


def parse_batch_translation(content, fragments):
    """Validate shape only. This cannot establish semantic translation quality."""
    if not isinstance(content, str):
        raise BatchValidationError('response_structure')
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except _DuplicateKey:
        raise BatchValidationError('json_duplicate_key') from None
    except (ValueError, RecursionError):
        raise BatchValidationError('json_invalid') from None
    if (not isinstance(value, dict) or set(value) != {'translations'} or
            not isinstance(value['translations'], dict)):
        raise BatchValidationError('response_structure')
    actual = len(value['translations'])
    expected = {str(i) for i in range(len(fragments))}
    translated = {}
    for identity, text in value['translations'].items():
        if identity not in expected:
            raise BatchValidationError('id_out_of_range', actual)
        if (not isinstance(text, str) or not text.strip() or any(c in text for c in ['-->', '```', '\x00']) or
                len(text) > max(500, len(fragments[int(identity)]) * 12) or
                any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\t\r' for c in text)):
            raise BatchValidationError('translation_invalid', actual)
        if any(c.isalnum() for c in fragments[int(identity)]) and not any(c.isalnum() for c in text):
            raise BatchValidationError('translation_invalid', actual)
        translated[identity] = text.strip()
    if set(translated) != expected:
        raise BatchValidationError('id_missing', actual)
    return {str(i): translated[str(i)] for i in range(len(fragments))}


def parse_batch_response(body, fragments):
    if not isinstance(body, dict):
        raise BatchValidationError('response_structure')
    if body.get('error') is not None:
        raise ToolError("翻译服务返回业务错误；进度已保存。", "mt_service_error")
    choices = body.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise BatchValidationError('response_structure')
    choice = choices[0]
    if (choice.get('error') is not None or choice.get('finish_reason') == 'error'):
        raise ToolError("翻译服务返回业务错误；进度已保存。", "mt_service_error")
    message = choice.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    # Retain the item count for truncated but parseable replies, without accepting them.
    if choice.get('finish_reason') != 'stop':
        try:
            count = len(parse_batch_translation(content, fragments))
        except BatchValidationError as error:
            count = error.actual_count
        raise BatchValidationError('unfinished', count)
    return parse_batch_translation(content, fragments)

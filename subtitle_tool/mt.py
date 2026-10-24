"""Versioned MT wire contracts; independent of persisted cue identities."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass

from .common import MT_INPUT_BUDGET, MT_MODEL, MT_TEMPLATE_RESERVE, ToolError
from .mt_backend import request_contract


PROMPT_VERSION = 9
RECOVERY_VERSION = 1
STRUCTURED_PROMPT_VERSION = 10
STRUCTURED_RECOVERY_VERSION = 2
STRUCTURED_PROMPT = (
    '把 targets 的字幕片段译成简体中文，用 context 消歧但不翻译 context。'
    '同一 sentence 内的片段连贯理解；每个编号仅翻译该片段已有的内容，'
    '允许短语不完整，不补全、不跨片重复，保留真实重复。'
    '必须返回 {"translations":{"0":"第0片译文","1":"第1片译文"}} 形式的 JSON，'
    '包含所有实际输入编号，译文非空。')
PROMPT = ("请把下面 JSON 中每个 text 字段的原文翻译成简体中文，保持 JSON 结构和 id 完全不变。"
          "所有 text 是同一句话的连续字幕片段，联合理解，但每条只翻译自己的文字，"
          "允许中文短语不完整、跨片段连读。不要补全、合并或重复邻片内容，保留原文真实重复。"
          "上面的背景只供消歧，严禁翻译或输出。仅输出翻译后的完整 JSON，各 text 必须非空，"
          "不加解释或 Markdown。")
REPAIR = "上次响应未满足编号 JSON 要求。请重新翻译，逐项返回全部编号及各自译文，严格遵守输出格式。\n"
FRAGMENT_EXAMPLE = ('分段边界示例（仅示范，不要输出示例）：\n'
                    '输入：{"translations":[{"id":"0","text":"She decided to"},{"id":"1","text":"wait until morning."}]}\n'
                    '输出：{"translations":[{"id":"0","text":"她决定"},{"id":"1","text":"等到早上。"}]}\n')


def batch_payload(fragments, source_lang="auto", context=(), *, repair=False, provider="bailian", model=None,
                  request_version=1):
    contract = request_contract(provider, request_version)
    if (not fragments or any(not isinstance(t, str) or not t.strip() for t in fragments) or
            not isinstance(source_lang, str) or not source_lang or
            any(not isinstance(c, dict) or set(c) != {"position", "text"} or
                c['position'] not in {'before', 'after', 'same_sentence_before', 'same_sentence_after'} or
                not isinstance(c['text'], str) or not c['text'].strip() for c in context)):
        raise ToolError("批量翻译输入或原文上下文无效。", "mt_context")
    if contract.structured_mapping:
        data = {'context': list(context),
                'targets': {str(i): {'sentence': 0, 'text': text} for i, text in enumerate(fragments)}}
        if source_lang != 'auto':
            data['source_language'] = source_lang
        mapping = {'type': 'object', 'properties': {str(i): {'type': 'string', 'minLength': 1}
                                                   for i in range(len(fragments))},
                   'required': [str(i) for i in range(len(fragments))], 'additionalProperties': False}
        payload = {'model': MT_MODEL if model is None else model, 'stream': False,
                   'messages': [{'role': 'system', 'content': STRUCTURED_PROMPT},
                                {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}],
                   'response_format': {'type': 'json_schema', 'json_schema': {
                       'name': 'subtitle_translations', 'strict': True,
                       'schema': {'type': 'object', 'properties': {'translations': mapping},
                                  'required': ['translations'], 'additionalProperties': False}}}}
        if contract.openrouter_controls:
            payload.update(provider={'require_parameters': True}, plugins=[{'id': 'response-healing'}])
        else:
            payload.update(enable_thinking=False, temperature=0, seed=0)
        return payload
    # Put background before the task, and keep id/text objects exclusive to targets.
    # Context following instructions can be mistaken for the translation target.
    background = '\n'.join(item['position'] + ': ' + item['text'] for item in context)
    data = {"translations": [{"id": str(i), "text": t} for i, t in enumerate(fragments)]}
    repair_instruction = ''
    if repair:
        shape = {'translations': [{'id': str(i), 'text': f'第{i}片段自己的译文'} for i in range(len(fragments))]}
        repair_instruction = (REPAIR + f"本次有 {len(fragments)} 个片段，必须返回 {len(fragments)} 项。"
                              "不得把完整句子放在第一项；请逐项填入译文，严格保留以下编号结构：\n" +
                              json.dumps(shape, ensure_ascii=False) + '\n')
    content = ("以下是仅供理解的原文上下文，不需要翻译：\n" + background + '\n\n' + PROMPT +
               f"源语言：{source_lang}。\n" + repair_instruction +
               (FRAGMENT_EXAMPLE + "下面才是本次待翻译的数据：\n" if len(fragments) > 1 else "") +
               "INPUT_JSON:\n" + json.dumps(data, ensure_ascii=False))
    # The documented custom-prompt mode carries language instructions in the user message.
    payload = {"model": MT_MODEL if model is None else model,
               "messages": [{"role": "user", "content": content}], "stream": False}
    if provider == "bailian":
        payload.update(temperature=0, seed=0)
    if contract.openrouter_controls:
        payload.update(
            response_format={"type": "json_schema", "json_schema": {
                "name": "subtitle_translations", "strict": True,
                "schema": {"type": "object", "properties": {
                    "translations": {"type": "array", "items": {
                        "type": "object", "properties": {
                            "id": {"type": "string", "enum": [str(i) for i in range(len(fragments))]},
                            "text": {"type": "string"}},
                        "required": ["id", "text"], "additionalProperties": False}}},
                    "required": ["translations"], "additionalProperties": False}}},
            provider={"require_parameters": True}, plugins=[{"id": "response-healing"}])
    return payload


def batch_input_size(fragments, source_lang="auto", context=(), *, request_options=None):
    # Reserve the legacy repair prompt; structured requests use one unchanged payload.
    return len(json.dumps(batch_payload(fragments, source_lang, context, repair=True, **(request_options or {})),
                          ensure_ascii=False).encode('utf-8')) + MT_TEMPLATE_RESERVE


def validate_batch_input(fragments, source_lang="auto", context=(), *, request_options=None):
    if batch_input_size(fragments, source_lang, context, request_options=request_options) > MT_INPUT_BUDGET:
        raise ToolError("待翻译文本超过保守输入预算，且无法在可靠词时间边界安全拆分；进度已保存，可恢复。", "mt_input_budget")


@dataclass(frozen=True)
class BatchTranslation:
    translations: dict[str, str]
    recovered: bool


VALIDATION_CATEGORIES = {
    'response_structure': '响应外层结构异常', 'json_invalid': 'JSON 无效',
    'json_duplicate_key': 'JSON 重复键', 'id_missing': '编号缺失',
    'id_duplicate': '编号重复', 'id_out_of_range': '编号越界',
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


def parse_batch_translation(content, fragments, *, request_version=1):
    """Validate shape only. This cannot establish semantic translation quality."""
    if not isinstance(content, str):
        raise BatchValidationError('response_structure')
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except _DuplicateKey:
        raise BatchValidationError('json_duplicate_key') from None
    except (ValueError, RecursionError):
        raise BatchValidationError('json_invalid') from None
    mapped = request_contract('openai_compatible', request_version).structured_mapping
    if (not isinstance(value, dict) or set(value) != {'translations'} or
            not isinstance(value['translations'], dict if mapped else list)):
        raise BatchValidationError('response_structure')
    actual = len(value['translations'])
    expected = {str(i) for i in range(len(fragments))}
    translated = {}
    items = ([{'id': key, 'text': text} for key, text in value['translations'].items()] if mapped else value['translations'])
    for item in items:
        if not isinstance(item, dict):
            raise BatchValidationError('response_structure', actual)
        if 'id' not in item:
            raise BatchValidationError('id_missing', actual)
        if set(item) != {'id', 'text'}:
            raise BatchValidationError('translation_invalid', actual)
        identity, text = item['id'], item['text']
        if not isinstance(identity, str) or identity not in expected:
            raise BatchValidationError('id_out_of_range', actual)
        if identity in translated:
            raise BatchValidationError('id_duplicate', actual)
        if (not isinstance(text, str) or not text.strip() or any(c in text for c in ['-->', '```', '\x00']) or
                len(text) > max(500, len(fragments[int(identity)]) * 12) or
                mapped and any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\t\r' for c in text)):
            raise BatchValidationError('translation_invalid', actual)
        if any(c.isalnum() for c in fragments[int(identity)]) and not any(c.isalnum() for c in text):
            raise BatchValidationError('translation_invalid', actual)
        translated[identity] = text.strip()
    if set(translated) != expected:
        raise BatchValidationError('id_missing', actual)
    return {str(i): translated[str(i)] for i in range(len(fragments))}


def parse_batch_response(body, fragments, *, request_version=1):
    contract = request_contract('openai_compatible', request_version)
    if not isinstance(body, dict):
        raise BatchValidationError('response_structure')
    if contract.bounded_runtime and body.get('error') is not None:
        raise ToolError("翻译服务返回业务错误；进度已保存。", "mt_service_error")
    choices = body.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise BatchValidationError('response_structure')
    choice = choices[0]
    if contract.bounded_runtime and (choice.get('error') is not None or choice.get('finish_reason') == 'error'):
        raise ToolError("翻译服务返回业务错误；进度已保存。", "mt_service_error")
    message = choice.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    # Historical requests checked content first; version 2 classifies truncation even without text.
    if request_version == 1 and not isinstance(content, str):
        raise BatchValidationError('response_structure')
    # Retain the item count for truncated but parseable replies, without accepting them.
    if choice.get('finish_reason') != 'stop':
        try:
            count = len(parse_batch_translation(content, fragments, request_version=request_version))
        except BatchValidationError as error:
            count = error.actual_count
        raise BatchValidationError('unfinished', count)
    return parse_batch_translation(content, fragments, request_version=request_version)


def recovery_context(fragments, start, end, source_lang='auto', context=(), *, request_options=None):
    """Keep nearest original siblings first, then the original ordered background."""
    siblings = sorted([(start - i, i, 'same_sentence_before') for i in range(start)] +
                      [(i - end + 1, i, 'same_sentence_after') for i in range(end, len(fragments))])
    candidates = [{'position': side, 'text': fragments[i]} for _, i, side in siblings] + list(context)
    kept, seen = [], set()
    for item in candidates:
        identity = (item['position'], item['text'])
        if identity not in seen:
            kept.append(item)
            seen.add(identity)
    targets = fragments[start:end]
    while kept and batch_input_size(targets, source_lang, kept, request_options=request_options) > MT_INPUT_BUDGET:
        kept.pop()
    validate_batch_input(targets, source_lang, kept, request_options=request_options)
    return kept


def batch_cache_value(translated):
    return json.dumps({'translations': [{'id': key, 'text': text} for key, text in translated.items()]},
                      ensure_ascii=False)


def batch_cache_key(fragments, source_lang="auto", *, context=(), stage="subtitle", asr_language="", request_options=None, **scope):
    options = request_options or {}
    structured = request_contract(options.get('provider', 'bailian'), options.get('request_version', 1)).structured_mapping
    value = {"cache_version": 4, "strategy_version": 4 if structured else 3,
             "prompt_version": STRUCTURED_PROMPT_VERSION if structured else PROMPT_VERSION,
             "request": batch_payload(fragments, source_lang, context, **options), "stage": stage,
             "asr_language": asr_language, "scope": scope, "simplification": "opencc-t2s-0.1.7"}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def batch_recovery_cache_key(batch_key):
    value = {'recovery_version': RECOVERY_VERSION, 'batch_key': batch_key}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()

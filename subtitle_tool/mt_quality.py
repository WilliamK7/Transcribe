"""Version 5 stage prompts, strict local validation and source-aware grouping.

Text and review evidence returned by these helpers belong in private task storage,
never in diagnostics or ordinary reports.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from opencc import OpenCC

from .common import ToolError
from .review import decision_origin


VALIDATION_CATEGORIES = {
    'response_structure': '响应外层结构异常', 'json_invalid': 'JSON 无效',
    'json_duplicate_key': 'JSON 重复键', 'id_missing': '编号缺失',
    'id_out_of_range': '编号越界',
    'translation_invalid': '译文无效', 'unfinished': '非正常结束',
}


class BatchValidationError(ToolError):
    def __init__(self, category, actual_count=None):
        super().__init__(VALIDATION_CATEGORIES[category] + '；未保存不完整结果。', 'mt_batch_invalid')
        self.category, self.actual_count = category, actual_count


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise BatchValidationError('json_duplicate_key')
        value[key] = item
    return value


def parse_content(content):
    """Decode one model JSON object without accepting duplicate fields."""
    if not isinstance(content, str):
        raise BatchValidationError('response_structure')
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise BatchValidationError('json_invalid') from None


def validate_translations(value, fragments):
    """Validate shape only. This cannot establish semantic translation quality."""
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


QUALITY_PROMPT_VERSION = 2
INPUT_BUDGET = 32 * 1024
OUTPUT_TOKENS = 4096
ISSUE_CATEGORIES = ('omission', 'addition', 'terminology', 'number', 'negation', 'condition', 'repetition', 'meaning')
PROMPTS = {
    'glossary': ('从课程原文提取有原句依据的技术术语、缩写和定义，给出简体中文译法。'
                 '仅提取输入句中实际出现的术语；sentence 填其原句 id。人工术语优先；不要根据歧义机械统一。'
                 '没有可确定术语时返回空 terms 数组。仅返回 JSON。'),
    'draft': ('逐个完整句译为准确自然的简体中文，以术语表和上下文消歧，忠实保留数字、单位、否定和条件。'
              '术语有语境歧义时不要机械替换。只翻译 sentences，不翻译上下文。'
              '返回 translations 对象，键与输入句的 id 一一对应，译文非空。仅返回 JSON。'),
    'align': ('依据已完成的整句译文，把译文对齐到各字幕片段；保持完整含义，允许跨片连读，'
              '不增删或跨片重复内容。返回 translations 对象，键与 fragments 的 id 一一对应，译文非空。仅返回 JSON。'),
    'review': ('复核原句、完整译文和字幕片段：漏译、增译、术语、数字单位、否定条件和跨片重复。'
               '只报告具体可定位的疑点，不要猜测；每个疑点含片段 id、类别及具体依据。'
               '没有疑点返回空 issues 数组。仅返回 JSON。'),
    'correct': ('根据给出的具体疑点修正整个字幕组，保持原文的完整含义和片段顺序。'
                '返回 translations 对象，键与 fragments 的 id 一一对应，译文非空。仅返回 JSON。'),
}


def plan_sentence_groups(sources):
    """Only consecutive eligible sources with the same segment and review boundary."""
    groups, current = [], []
    identity = None
    for source in sources:
        if source['decision'] != 'translate':
            if current:
                groups.append(current)
                current = []
            identity = None
            continue
        key = (source['sentence_id'].split(':', 1)[0], source['language'].strip().lower(),
               source.get('source_lang', 'auto'), decision_origin(source))
        if (current and (key != identity or len(current) >= 8 or
                         source['end_ms'] - current[0]['begin_ms'] > 60000)):
            groups.append(current)
            current = []
        if not current:
            identity = key
        current.append(source)
    if current:
        groups.append(current)
    return groups


def _schema(stage, data):
    if stage == 'glossary':
        item = {'type': 'object', 'properties': {'source': {'type': 'string'}, 'translation': {'type': 'string'},
                                                'sentence': {'type': 'string'}},
                'required': ['source', 'translation', 'sentence'], 'additionalProperties': False}
        item['properties']['sentence']['enum'] = [str(s['id']) for s in data['sentences']]
        return {'type': 'object', 'properties': {'terms': {'type': 'array', 'items': item}},
                'required': ['terms'], 'additionalProperties': False}
    if stage == 'review':
        item = {'type': 'object', 'properties': {'id': {'type': 'string'},
                'category': {'type': 'string', 'enum': list(ISSUE_CATEGORIES)}, 'evidence': {'type': 'string'}},
                'required': ['id', 'category', 'evidence'], 'additionalProperties': False}
        item['properties']['id']['enum'] = [str(f['id']) for f in data['fragments']]
        return {'type': 'object', 'properties': {'issues': {'type': 'array', 'items': item}},
                'required': ['issues'], 'additionalProperties': False}
    items = data['sentences'] if stage == 'draft' else data['fragments']
    ids = [str(item['id']) for item in items]
    mapping = {'type': 'object', 'properties': {key: {'type': 'string'} for key in ids},
               'required': ids, 'additionalProperties': False}
    return {'type': 'object', 'properties': {'translations': mapping},
            'required': ['translations'], 'additionalProperties': False}


def make_stage_payload(stage, data, backend, *, capabilities):
    if stage not in PROMPTS:
        raise ValueError('Unknown translation stage')
    if (not isinstance(capabilities, dict) or set(capabilities) != {'response_format', 'input_bytes', 'output_tokens'} or
            capabilities['response_format'] not in {'json_object', 'json_schema', 'text'} or
            type(capabilities['input_bytes']) is not int or not 1 <= capabilities['input_bytes'] <= 1048576 or
            type(capabilities['output_tokens']) is not int or not 1 <= capabilities['output_tokens'] <= 32768):
        raise ToolError('翻译模型能力档案无效。', 'mt_config')
    prompt = PROMPTS[stage]
    prompt += '直接返回所需 JSON，不附解释、Markdown 或思考过程。'
    if stage == 'review':
        prompt += ('审核对象是 translations 中按 fragments 编号排列的最终字幕，结合完整原句和跨片连读判断。'
                   '不要把正常分片、同义表达或与 draft 措辞不同本身当作错误。'
                   '合并重复疑点，每条 evidence 用一句简短文字指出原文与译文的具体差异。')
    if capabilities['response_format'] != 'json_schema':
        prompt += '输出必须符合此 JSON Schema：' + json.dumps(
            _schema(stage, data), ensure_ascii=False, separators=(',', ':'))
    payload = {'model': backend.model, 'stream': False, 'max_tokens': min(capabilities['output_tokens'], OUTPUT_TOKENS),
               'messages': [{'role': 'system', 'content': prompt},
                            {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]}
    if capabilities['response_format'] == 'json_object':
        payload['response_format'] = {'type': 'json_object'}
    elif capabilities['response_format'] == 'json_schema':
        payload['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'subtitle_' + stage, 'strict': True, 'schema': _schema(stage, data)}}
    if backend.is_openrouter:
        payload.update(provider={'require_parameters': True}, plugins=[{'id': 'response-healing'}])
        if backend.model == 'openrouter/free':
            # Reasoning shares max_tokens with visible JSON; leave room for the result.
            payload['reasoning'] = {'effort': 'low'}
    elif backend.provider == 'bailian':
        payload.update(enable_thinking=False, temperature=0)
    elif (backend.provider == 'qiniu' and
          backend.model == 'deepseek/deepseek-v4.1-flash'):
        payload['reasoning_effort'] = 'low'
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    except (TypeError, UnicodeError, ValueError):
        raise ToolError('翻译阶段输入无效，进度已保存。', 'mt_context') from None
    if size > min(capabilities['input_bytes'], INPUT_BUDGET):
        raise ToolError('翻译阶段完整请求超过输入预算，进度已保存。', 'mt_input_budget')
    return payload


def stage_cache_key(stage, data, backend, *, capabilities, scope):
    value = {'version': QUALITY_PROMPT_VERSION, 'strategy': 5, 'simplification': 'opencc-t2s-0.1.7',
             'backend': backend.snapshot(),
             'capabilities': capabilities, 'scope': scope,
             'request': make_stage_payload(stage, data, backend, capabilities=capabilities)}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def parse_stage_response(stage, body, *, ids=(), originals=()):
    """Validate the wire envelope once; saved values use the same field validator."""
    try:
        if (not isinstance(body, dict) or body.get('error') is not None or
                not isinstance(body.get('choices'), list) or len(body['choices']) != 1 or
                not isinstance(body['choices'][0], dict) or body['choices'][0].get('error') is not None or
                isinstance(body['choices'][0].get('message'), dict) and body['choices'][0]['message'].get('refusal')):
            raise ValueError()
        choice = body['choices'][0]
        message = choice.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if choice.get('finish_reason') != 'stop':
            if stage not in {'draft', 'align', 'correct'}:
                raise ValueError()
            # Keep the count for truncated but parseable replies, without accepting them.
            try:
                count = len(validate_translations(parse_content(content), originals))
            except BatchValidationError as error:
                count = error.actual_count
            raise BatchValidationError('unfinished', count)
        return validate_stage_value(stage, parse_content(content), ids=ids, originals=originals)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise BatchValidationError('response_structure') from None


def validate_stage_value(stage, value, *, ids=(), originals=()):
    """Validate generated fields from either a response or private saved records."""
    try:
        if stage in {'draft', 'align', 'correct'}:
            if list(ids) != [str(i) for i in range(len(ids))] or len(originals) != len(ids):
                raise ValueError()
            translated = validate_translations(value, originals)
            cc = OpenCC('t2s')
            return {'translations': {key: cc.convert(text) for key, text in translated.items()}}
        key = 'terms' if stage == 'glossary' else 'issues' if stage == 'review' else None
        if key is None or not isinstance(value, dict) or set(value) != {key} or not isinstance(value[key], list):
            raise ValueError()
        if len(value[key]) > 200:
            raise ValueError()
        for item in value[key]:
            expected = {'source', 'translation', 'sentence'} if stage == 'glossary' else {'id', 'category', 'evidence'}
            if (not isinstance(item, dict) or set(item) != expected or
                    any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 2000 for k in expected) or
                    any(any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\t\r' for c in item[k])
                        for k in expected)):
                raise ValueError()
            if stage == 'review' and (item['id'] not in ids or item['category'] not in ISSUE_CATEGORIES):
                raise ValueError()
        if stage == 'glossary':
            cc = OpenCC('t2s')
            return {'terms': [{**item, 'translation': cc.convert(item['translation'])} for item in value['terms']]}
        return value
    except (KeyError, TypeError, ValueError, RecursionError):
        raise BatchValidationError('response_structure') from None


def validate_glossary(extracted, sources, manual):
    """Keep only evidenced terms, with per-sentence evidence and manual priority."""
    by_id = {source['sentence_id']: source['text'] for source in sources if source['decision'] == 'translate'}
    result, seen = [], set()
    def appears(term, text):
        if not term.isascii():
            return term.casefold() in text.casefold()
        pattern = re.escape(term)
        if term[0].isalnum() or term[0] == '_':
            pattern = r'(?<![A-Za-z0-9_])' + pattern
        if term[-1].isalnum() or term[-1] == '_':
            pattern += r'(?![A-Za-z0-9_])'
        return re.search(pattern, text, flags=re.IGNORECASE | re.ASCII) is not None
    def add(source, translation, sentence, origin):
        if (not isinstance(source, str) or not isinstance(translation, str) or not source.strip() or
                not translation.strip() or sentence not in by_id or not appears(source, by_id[sentence])):
            return
        identity = (sentence, source.casefold())
        if identity not in seen:
            result.append({'source': source, 'translation': translation, 'sentence': sentence, 'origin': origin})
            seen.add(identity)
    for source, translation in manual.items():
        for sentence in by_id:
            add(source, translation, sentence, 'manual')
    for item in extracted:
        if isinstance(item, dict):
            add(item.get('source'), item.get('translation'), item.get('sentence'), 'model')
    return result

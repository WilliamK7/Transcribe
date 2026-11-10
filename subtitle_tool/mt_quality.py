"""Stage prompts, response validation and source-aware grouping.

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
    'glossary_evidence': '术语缺少本次目标原文依据',
    'translation_invalid': '译文无效', 'unfinished': '非正常结束', 'output_truncated': '输出长度耗尽',
    'judge_evidence': '裁决没有保持已撤销疑点的译文',
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


QUALITY_PROMPT_VERSION = 5
GLOSSARY_SCHEDULE = 'window-prefix-v3'
INPUT_BUDGET = 32 * 1024
OUTPUT_TOKENS = 4096
ISSUE_CATEGORIES = ('omission', 'addition', 'terminology', 'number', 'negation', 'condition', 'repetition', 'meaning')
PROMPTS = {
    'glossary': ('从课程原文提取有原句依据的技术术语、缩写和定义，给出简体中文译法。'
                 '仅提取输入句中实际出现的术语；sentence 填其原句 id。人工术语优先；不要根据歧义机械统一。'
                 'glossary 是已确认术语，只补充新术语或有新语境依据的不同含义，不重复已有同义项。'
                 '没有可确定术语时返回空 terms 数组。仅返回 JSON。'),
    'translate': ('结合完整 sentences 及分片边界，一次译为准确自然的简体中文字幕。'
                  '以术语和上下文消歧，忠实保留数字、单位、否定和条件；只翻译目标，不翻译上下文。'
                  '按 fragments 顺序分配完整含义，允许跨片连读，不增删或重复内容。'
                  '返回 translations 对象，键与 fragments 的 id 一一对应，译文非空。仅返回 JSON。'),
    'review': ('复核最终 translations 的漏增译、术语、数字单位、否定条件和跨片重复。'
               '结合原句及上下文；正常分片和同义表达不算错误。'
               '疑点含片段 id、类别及一句具体差异，去重；无疑点返回空 issues 数组。'),
    'judge': ('独立核对 issues 中每条疑点是否有原文依据，撤销误报，不为措辞偏好修改译文。'
              '仅修正确有证据的问题及同句必要片段，其他编号原样保留。'
              '返回 issues（核实后的问题，误报不保留）和 translations（完整修正结果）。'
              '若全部为误报，issues 为空，translations 必须原样返回。'
              'translations 的键与 fragments 的 id 一一对应，译文非空。仅返回 JSON。'),
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


def stage_output_size(stage, data):
    """Estimate target workload, including translations and JSON field overhead."""
    targets = data['sentences'] if stage == 'glossary' else data['fragments']
    characters = sum(len(item['text']) for item in targets)
    characters = max(characters, sum(len(text) for text in data.get('translations', {}).values()))
    return characters + 32 * len(targets)


def estimate_stage_output(stage, data):
    """Estimate Chinese JSON output, adding room for advisory evidence fields."""
    targets = data['sentences'] if stage == 'glossary' else data['fragments']
    return 64 + stage_output_size(stage, data) + (64 * len(targets) if stage in {'glossary', 'review', 'judge'} else 0)


def correction_stalled(previous, current):
    """Detect repeated evidence with unchanged translations across affected sentences."""
    if previous is None or not current['issues']:
        return False
    for key in ('sentences', 'fragments', 'glossary', 'context'):
        if previous.get(key) != current.get(key):
            return False
    def issues(data):
        return {(item['id'], item['category'], item['evidence']) for item in data['issues']}
    if issues(previous) != issues(current):
        return False
    flagged = {item['id'] for item in current['issues']}
    affected = {item['sentence'] for item in current['fragments'] if item['id'] in flagged}
    return all(previous['translations'][item['id']] == current['translations'][item['id']]
               for item in current['fragments'] if item['sentence'] in affected)


def _schema(stage, data):
    if stage == 'judge':
        schema = _schema('translate', data)
        schema['properties'].update(_schema('review', data)['properties'])
        schema['required'].append('issues')
        return schema
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
    items = data['fragments']
    ids = [str(item['id']) for item in items]
    mapping = {'type': 'object', 'properties': {key: {'type': 'string'} for key in ids},
               'required': ids, 'additionalProperties': False}
    return {'type': 'object', 'properties': {'translations': mapping},
            'required': ['translations'], 'additionalProperties': False}


def make_stage_payload(stage, data, backend, *, capabilities, repair=False):
    if stage not in PROMPTS:
        raise ValueError('Unknown translation stage')
    if (not isinstance(capabilities, dict) or set(capabilities) != {'response_format', 'input_bytes', 'output_tokens'} or
            capabilities['response_format'] != 'json_object' or
            type(capabilities['input_bytes']) is not int or not 1 <= capabilities['input_bytes'] <= 1048576 or
            type(capabilities['output_tokens']) is not int or not 1 <= capabilities['output_tokens'] <= 32768):
        raise ToolError('翻译模型能力档案无效。', 'mt_config')
    prompt = PROMPTS[stage]
    prompt += '直接返回所需 JSON，不附解释、Markdown 或思考过程。'
    if data.get('context'):
        prompt += ('context 只供消歧，不翻译或提取术语；same_sentence 的 sentence 指目标编号，'
                   '文字属于同句其他范围。')
    if data.get('glossary'):
        prompt += ('术语表中 manual 为人工指定译法；model 条目附原文 evidence，仅供对应语境参考，'
                   '同词可有多个译法，按当前语境选择，不因较早出现而强制统一。')
    if repair:
        if stage != 'translate':
            raise ValueError('Only translation allows format repair')
        prompt += ('上次响应未通过格式或编号校验。请从本次原始任务重新生成完整 JSON：'
                   '每个目标编号恰好一次，译文完整且非空，不添加其他编号或说明。')
    prompt += '输出必须符合此 JSON Schema：' + json.dumps(
        _schema(stage, data), ensure_ascii=False, separators=(',', ':'))
    # Retention priority is internal; model input always states the original order.
    wire_data = dict(data)
    if data.get('context'):
        context = data['context']
        if all(isinstance(item, dict) and 'source_position' in item for item in context):
            context = sorted(context, key=lambda item: (item['source_position'], item.get('char_start', 0)))
        targets = {item.get('source_id'): item['id'] for item in data.get('sentences', []) if item.get('source_id')}
        wire_context = []
        for item in context:
            if not isinstance(item, dict):
                wire_context.append(item)
                continue
            item = {key: value for key, value in item.items() if key not in {'required', 'source_position'}}
            if item.get('kind') == 'same_sentence' and item.get('sentence_id') in targets:
                item['sentence'] = targets[item.pop('sentence_id')]
                item.pop('position', None)  # Character ranges already describe the side of this target.
            wire_context.append(item)
        wire_data['context'] = wire_context
    payload = {'model': backend.model, 'stream': False, 'max_tokens': min(capabilities['output_tokens'], OUTPUT_TOKENS),
               'reasoning_effort': 'low' if stage == 'judge' else 'none',
               'messages': [{'role': 'system', 'content': prompt},
                             {'role': 'user', 'content': json.dumps(wire_data, ensure_ascii=False)}]}
    payload['response_format'] = {'type': 'json_object'}
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    except (TypeError, UnicodeError, ValueError):
        raise ToolError('翻译阶段输入无效，进度已保存。', 'mt_context') from None
    if size > min(capabilities['input_bytes'], INPUT_BUDGET):
        raise ToolError('翻译阶段完整请求超过输入预算，进度已保存。', 'mt_input_budget')
    return payload


def stage_cache_key(stage, data, backend, *, capabilities, scope):
    value = {'version': QUALITY_PROMPT_VERSION, 'strategy': 6, 'simplification': 'opencc-t2s-0.1.7',
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
            truncated = choice.get('finish_reason') == 'length'
            if stage not in {'translate', 'judge'}:
                if truncated:
                    raise BatchValidationError('output_truncated')
                raise ValueError()
            # Keep the count for truncated but parseable replies, without accepting them.
            try:
                count = len(validate_translations(parse_content(content), originals))
            except BatchValidationError as error:
                count = error.actual_count
            raise BatchValidationError('output_truncated' if truncated else 'unfinished', count)
        return validate_stage_value(stage, parse_content(content), ids=ids, originals=originals)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise BatchValidationError('response_structure') from None


def validate_stage_value(stage, value, *, ids=(), originals=()):
    """Validate generated fields from either a response or private saved records."""
    try:
        if stage == 'judge':
            if not isinstance(value, dict) or set(value) != {'issues', 'translations'}:
                raise ValueError()
            return {**validate_stage_value('translate', {'translations': value['translations']},
                                           ids=ids, originals=originals),
                    **validate_stage_value('review', {'issues': value['issues']}, ids=ids)}
        if stage == 'translate':
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
        if stage == 'glossary':
            if len(ids) != len(originals) or len(set(ids)) != len(ids):
                raise ValueError()
            targets = dict(zip(ids, originals))
        for item in value[key]:
            expected = {'source', 'translation', 'sentence'} if stage == 'glossary' else {'id', 'category', 'evidence'}
            if (not isinstance(item, dict) or set(item) != expected or
                    any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 2000 for k in expected) or
                    any(any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\t\r' for c in item[k])
                        for k in expected)):
                raise ValueError()
            if stage == 'review' and (item['id'] not in ids or item['category'] not in ISSUE_CATEGORIES):
                raise ValueError()
            if stage == 'glossary' and (item['sentence'] not in targets or
                    not term_appears(item['source'], targets[item['sentence']])):
                raise BatchValidationError('glossary_evidence')
        if stage == 'glossary':
            cc = OpenCC('t2s')
            return {'terms': [{**item, 'translation': cc.convert(item['translation'])} for item in value['terms']]}
        return value
    except (KeyError, TypeError, ValueError, RecursionError):
        raise BatchValidationError('response_structure') from None


def validate_judgment(value, data):
    if not value['issues'] and value['translations'] != data['translations']:
        raise BatchValidationError('judge_evidence')
    return value


def _ascii_term_pattern(term):
    pattern = re.escape(term)
    if term[0].isalnum() or term[0] == '_':
        pattern = r'(?<![A-Za-z0-9_])' + pattern
    if term[-1].isalnum() or term[-1] == '_':
        pattern += r'(?![A-Za-z0-9_])'
    return pattern


def term_appears(term, text):
    if not term.isascii():
        return term.casefold() in text.casefold()
    return re.search(_ascii_term_pattern(term), text, flags=re.IGNORECASE | re.ASCII) is not None


def term_evidence(term, text):
    pattern = _ascii_term_pattern(term) if term.isascii() else re.escape(term)
    match = re.search(pattern, text, flags=re.IGNORECASE | (re.ASCII if term.isascii() else 0))
    # Some Unicode case folds change length. Keeping the original source in that
    # rare case avoids inventing character offsets for the evidence window.
    return text[max(0, match.start() - 80):match.end() + 80] if match is not None else text


def manual_terms(sources, glossary):
    """Locate configured manual terms in translated targets, in configuration order."""
    unique = {}
    for term, translation in glossary.items():
        unique.setdefault(term.casefold(), (term, translation))
    return [{'source': term, 'translation': translation, 'sentence': source['sentence_id'], 'origin': 'manual'}
            for term, translation in unique.values() for source in sources
            if source['decision'] == 'translate' and term_appears(term, source['text'])]

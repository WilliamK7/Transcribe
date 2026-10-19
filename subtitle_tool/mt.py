"""Version 3 MT wire contract; independent of persisted cue identities."""
from __future__ import annotations

import hashlib
import json

from .common import MT_INPUT_BUDGET, MT_MODEL, MT_TEMPLATE_RESERVE, ToolError


PROMPT_VERSION = 9
PROMPT = ("请把下面 JSON 中每个 text 字段的原文翻译成简体中文，保持 JSON 结构和 id 完全不变。"
          "所有 text 是同一句话的连续字幕片段，联合理解，但每条只翻译自己的文字，"
          "允许中文短语不完整、跨片段连读。不要补全、合并或重复邻片内容，保留原文真实重复。"
          "上面的背景只供消歧，严禁翻译或输出。仅输出翻译后的完整 JSON，各 text 必须非空，"
          "不加解释或 Markdown。")
REPAIR = "上次响应未满足编号 JSON 要求。请重新翻译，逐项返回全部编号及各自译文，严格遵守输出格式。\n"
FRAGMENT_EXAMPLE = ('分段边界示例（仅示范，不要输出示例）：\n'
                    '输入：{"translations":[{"id":"0","text":"She decided to"},{"id":"1","text":"wait until morning."}]}\n'
                    '输出：{"translations":[{"id":"0","text":"她决定"},{"id":"1","text":"等到早上。"}]}\n')


def batch_payload(fragments, source_lang="auto", context=(), *, repair=False):
    if (not fragments or any(not isinstance(t, str) or not t.strip() for t in fragments) or
            not isinstance(source_lang, str) or not source_lang or
            any(not isinstance(c, dict) or set(c) != {"position", "text"} or
                c['position'] not in {'before', 'after', 'same_sentence_before', 'same_sentence_after'} or
                not isinstance(c['text'], str) or not c['text'].strip() for c in context)):
        raise ToolError("批量翻译输入或原文上下文无效。", "mt_context")
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
    return {"model": MT_MODEL, "messages": [{"role": "user", "content": content}],
            "stream": False, "temperature": 0, "seed": 0}


def batch_input_size(fragments, source_lang="auto", context=()):
    # Include the repair instruction so a second attempt always fits the same budget.
    return len(json.dumps(batch_payload(fragments, source_lang, context, repair=True),
                          ensure_ascii=False).encode('utf-8')) + MT_TEMPLATE_RESERVE


def validate_batch_input(fragments, source_lang="auto", context=()):
    if batch_input_size(fragments, source_lang, context) > MT_INPUT_BUDGET:
        raise ToolError("待翻译文本超过保守输入预算，且无法在可靠词时间边界安全拆分；进度已保存，可恢复。", "mt_input_budget")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def parse_batch_translation(content, fragments):
    """Validate shape only. This cannot establish semantic translation quality."""
    value = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {'translations'} or not isinstance(value['translations'], list):
        raise ValueError('invalid translations')
    expected = {str(i) for i in range(len(fragments))}
    translated = {}
    for item in value['translations']:
        if not isinstance(item, dict) or set(item) != {'id', 'text'}:
            raise ValueError('invalid translation item')
        identity, text = item['id'], item['text']
        if (not isinstance(identity, str) or identity not in expected or identity in translated or
                not isinstance(text, str) or not text.strip() or any(c in text for c in ['-->', '```', '\x00']) or
                len(text) > max(500, len(fragments[int(identity)]) * 12)):
            raise ValueError('invalid translation id or text')
        if any(c.isalnum() for c in fragments[int(identity)]) and not any(c.isalnum() for c in text):
            raise ValueError('translation has no text')
        translated[identity] = text.strip()
    if set(translated) != expected:
        raise ValueError('incomplete translations')
    return {str(i): translated[str(i)] for i in range(len(fragments))}


def batch_cache_value(translated):
    return json.dumps({'translations': [{'id': key, 'text': text} for key, text in translated.items()]},
                      ensure_ascii=False)


def batch_cache_key(fragments, source_lang="auto", *, context=(), stage="subtitle", asr_language="", **scope):
    value = {"cache_version": 4, "strategy_version": 3, "prompt_version": PROMPT_VERSION,
             "request": batch_payload(fragments, source_lang, context), "stage": stage,
             "asr_language": asr_language, "scope": scope, "simplification": "opencc-t2s-0.1.7"}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

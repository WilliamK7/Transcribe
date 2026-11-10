"""Bounded synthetic model admission. No user transcript enters this suite."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from types import SimpleNamespace

from .common import Cancelled, ToolError
from .mt_quality import make_stage_payload, parse_stage_response, parse_content, validate_judgment
from .mt_transport import TransportError
from .mt_transport import ProcessTransport

SUITE_VERSION = 2
CAPABILITIES = {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}
ADMISSION_TIMEOUT = 120  # Bounded total time for one synthetic request through the gateway.
# Four independent cases per role, twelve candidate calls at most.
CASES = (
    ('Do not disable the cache. It stores 12 results.', ['Do not disable the cache.', 'It stores 12 results.'],
     ['不要禁用缓存。', '它存储 12 个结果。'], '禁用缓存。', 'negation'),
    ('If the temperature exceeds 35 degrees, stop the pump.', ['If the temperature exceeds 35 degrees,', 'stop the pump.'],
     ['如果温度超过 35 度，', '停止水泵。'], '如果温度低于 35 度，', 'condition'),
    ('The API returns 3 records, not 30.', ['The API returns 3 records, not 30.'],
     ['API 返回 3 条记录，而不是 30 条。'], 'API 返回 30 条记录。', 'number'),
    ('A cache stores computed results; a cache miss needs another calculation.',
     ['A cache stores computed results;', 'a cache miss needs another calculation.'],
     ['缓存存储已计算的结果；', '缓存未命中时需要重新计算。'], '缓存未命中时无需重新计算。', 'meaning'),
)


def canonical_model(model):
    """Collapse vendor, effort and dated aliases to the same model family."""
    model = re.sub(r':(?:none|minimal|low|medium|high|xhigh|max|free)$', '', model.lower())
    model = model.rsplit('/', 1)[-1].replace('_', '-')
    return re.sub(r'-(?:20\d{2}-\d{2}-\d{2}|\d{4})$', '', model)


def case_data(index):
    source, fragments, targets, bad, category = CASES[index]
    return {'sentences': [{'id': '0', 'text': source}],
            'fragments': [{'id': str(i), 'sentence': '0', 'text': text} for i, text in enumerate(fragments)],
            'context': [], 'glossary': [{'source': 'cache', 'translation': '缓存', 'sentence': '0',
                                      'origin': 'manual', 'evidence': source}] if 'cache' in source else []}


class ModelEvaluator:
    def __init__(self, config, control, *, judge, transport=None):
        self.config, self.control, self.judge = config, control, judge
        self.transport = transport or ProcessTransport()
        self.transient = False

    def request(self, model, stage, data):
        payload = make_stage_payload(stage, data, SimpleNamespace(model=model), capabilities=CAPABILITIES)
        reply = self.transport.request('POST', self.config.settings.mt_base_url + '/chat/completions',
            headers={'Authorization': 'Bearer ' + self.config.get_magpie_key(), 'Content-Type': 'application/json',
                     'X-Magpie-Response-Model': 'member', 'X-Magpie-Session': 'transcribe-admission-' + uuid.uuid4().hex},
            json=payload, control=self.control, timeout=ADMISSION_TIMEOUT, drain=False)
        if reply.status != 200:
            raise TransportError('http_transient')
        return json.loads(reply.body)

    def evaluate(self, model):
        self.transient = False
        roles, samples, actual = [], {}, set()
        reasoning, transient = {s: [] for s in ('translate', 'review', 'judge')}, False
        started = time.monotonic()
        for stage in ('translate', 'review', 'judge'):
            passed = True
            samples[stage] = []
            for index in range(4):
                self.control.check_start()
                data = case_data(index)
                ids = [f['id'] for f in data['fragments']]
                good = dict(zip(ids, CASES[index][2]))
                if stage != 'translate':
                    data['translations'] = dict(good)
                    if index % 2:
                        data['translations'][ids[-1] if index == 3 else '0'] = CASES[index][3]
                    if stage == 'judge':
                        data['issues'] = [{'id': ids[-1] if index == 3 else '0', 'category': CASES[index][4],
                                           'evidence': '请核对数字、否定或条件关系。'}]
                try:
                    body = self.request(model, stage, data)
                    value = parse_stage_response(stage, body, ids=ids, originals=[f['text'] for f in data['fragments']])
                    name = body.get('model')
                    if not isinstance(name, str) or not name or name.startswith('group/'):
                        raise ToolError('验收未返回实际型号。', 'mt_admission')
                    actual.add(canonical_model(name))
                    usage = body.get('usage') or {}
                    details = usage.get('completion_tokens_details') or {}
                    reasoning[stage].append(details.get('reasoning_tokens'))
                    if stage == 'translate':
                        text = ' '.join(value['translations'].values())
                        if set(re.findall(r'\d+', CASES[index][0])) != set(re.findall(r'\d+', text)):
                            passed = False
                        if 'cache' in CASES[index][0] and '缓存' not in text:
                            passed = False
                        samples[stage].append({'source': CASES[index][0], 'translations': value['translations']})
                    elif bool(value['issues']) != bool(index % 2):
                        passed = False
                    if stage == 'judge':
                        validate_judgment(value, data)
                        samples[stage].append({'source': CASES[index][0], 'translations': value['translations']})
                except Cancelled:
                    raise
                except TransportError:
                    transient, passed = True, False
                    break
                except (ToolError, ValueError, TypeError, KeyError):
                    passed = False
                    break
            if passed:
                roles.append(stage)
            if transient:
                break  # A refused candidate must not be sent again for the next role.
        # A separate established judge evaluates facts and naturalness in one call.
        checked_roles = [r for r in ('translate', 'judge') if r in roles]
        cases = [case for r in checked_roles for case in samples[r]]
        if cases and not transient:
            accepted = self._judge(cases, actual) if canonical_model(self.judge) not in actual else []
            for index, role in enumerate(checked_roles):
                if accepted[index * 4:(index + 1) * 4] != [True] * 4:
                    roles.remove(role)
        non_thinking = {stage: len(values) == 4 and all(type(n) is int and n == 0 for n in values)
                        for stage, values in reasoning.items()}
        return {'roles': [] if transient or self.transient else roles, 'actual_models': sorted(actual),
                'non_thinking': non_thinking, 'reasoning_tokens': reasoning,
                'retry_after': time.time() + 300 if transient or self.transient else None,
                'duration_ms': round((time.monotonic() - started) * 1000), 'suite': SUITE_VERSION}

    def _judge(self, cases, actual):
        payload = {'model': self.judge, 'stream': False, 'max_tokens': 1024, 'reasoning_effort': 'none',
            'response_format': {'type': 'json_object'}, 'messages': [
                {'role': 'system', 'content': '逐条核对 source 与 translations 是否忠实完整、自然，特别检查数字、条件、否定、缓存术语和跨片重复。'
                    f'输入顶层数组有 {len(cases)} 个元素，每个元素是一条完整原句，translations 中各片段合起来评价一次。'
                    f'仅返回 {{"pass":[true或false,...]}}，数组必须恰好 {len(cases)} 项并对应输入顶层顺序，不按字幕片段单独计数；不可宽容语义错误。'},
                {'role': 'user', 'content': json.dumps(cases, ensure_ascii=False)}]}
        try:
            reply = self.transport.request('POST', self.config.settings.mt_base_url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + self.config.get_magpie_key(), 'Content-Type': 'application/json',
                         'X-Magpie-Response-Model': 'member',
                         'X-Magpie-Session': 'transcribe-admission-judge-' + uuid.uuid4().hex},
                json=payload, control=self.control, timeout=ADMISSION_TIMEOUT, drain=False)
            if reply.status != 200:
                self.transient = True
                return []
            body = json.loads(reply.body)
            choice = body['choices'][0]
            value = parse_content(choice['message']['content'])
            if (reply.status == 200 and choice['finish_reason'] == 'stop' and
                    isinstance(body.get('model'), str) and not body['model'].startswith('group/') and
                    canonical_model(body['model']) not in actual and isinstance(value, dict) and
                    set(value) == {'pass'} and isinstance(value['pass'], list) and
                    len(value['pass']) == len(cases) and all(type(v) is bool for v in value['pass'])):
                return value['pass']
            return []
        except Cancelled:
            raise
        except TransportError:
            self.transient = True
            return []
        except (ToolError, ValueError, TypeError, KeyError, IndexError):
            return []


def admission_key(provider, model):
    # Include credential identity without persisting the key.
    fields = {key: provider.get(key) for key in ('chat', 'key', 'headers', 'preset')}
    return hashlib.sha256(json.dumps({'model': model, 'provider': fields,
        'suite': SUITE_VERSION, 'capabilities': CAPABILITIES, 'effort': ['none', 'low']},
        sort_keys=True).encode()).hexdigest()

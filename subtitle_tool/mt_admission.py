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
from .mt_transport import ProcessTransport, TransportError
from .mt_identity import resolve_model, model_set
from .mt_channel_policy import check_provider_policy

SUITE_VERSION = 3
CAPABILITIES = {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}
ADMISSION_TIMEOUT = 120  # Bounded total time for one synthetic request through the gateway.
SAFE_SERVICE_CODES = {'model_not_supported', 'model_not_found', 'invalid_api_key', 'insufficient_quota',
                      'rate_limit_exceeded', 'insufficient_credits', 'authentication_error', 'permission_denied',
                      'unsupported_parameter', 'invalid_request_error', 'client_not_supported'}
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


class AdmissionHTTPError(ToolError):
    def __init__(self, status):
        super().__init__('候选接口拒绝了验收请求。', 'mt_admission_http')
        self.retryable = status in {408, 429} or status >= 500


def case_data(index):
    source, fragments, targets, bad, category = CASES[index]
    return {'sentences': [{'id': '0', 'text': source}],
            'fragments': [{'id': str(i), 'sentence': '0', 'text': text} for i, text in enumerate(fragments)],
            'context': [], 'glossary': [{'source': 'cache', 'translation': '缓存', 'sentence': '0',
                                      'origin': 'manual', 'evidence': source}] if 'cache' in source else []}


class ModelEvaluator:
    def __init__(self, config, control, *, judge, transport=None, identities=None):
        self.config, self.control, self.judge = config, control, judge
        self.transport = transport or ProcessTransport()
        self.transient = False
        self.identities = identities or {}
        self.failures = []

    def request(self, model, stage, data):
        check_provider_policy(self.config, model)
        payload = make_stage_payload(stage, data, SimpleNamespace(model=model), capabilities=CAPABILITIES)
        reply = self.transport.request('POST', self.config.settings.mt_base_url + '/chat/completions',
            headers={'Authorization': 'Bearer ' + self.config.get_magpie_key(), 'Content-Type': 'application/json',
                     'X-Magpie-Response-Model': 'member', 'X-Magpie-Session': 'transcribe-admission-' + uuid.uuid4().hex},
            json=payload, control=self.control, timeout=ADMISSION_TIMEOUT, drain=False)
        if reply.status != 200:
            failure = {'stage': stage, 'status': reply.status, 'category': 'http'}
            try:
                code = json.loads(reply.body).get('error', {}).get('code')
                if isinstance(code, str) and code in SAFE_SERVICE_CODES:
                    failure['service_code'] = code
            except (ValueError, TypeError, AttributeError):
                pass
            self.failures.append(failure)
            raise AdmissionHTTPError(reply.status)
        body = json.loads(reply.body)
        if not isinstance(body, dict):
            raise ValueError()
        if 'model' in body:
            body['_resolved_model'] = resolve_model(body['model'], reply.headers, self.identities)
        return body

    def evaluate(self, model):
        check_provider_policy(self.config, [model, self.judge])
        self.transient = False
        self.failures = []
        roles, samples, actual = [], {}, set()
        possible = model_set(model, self.identities)
        dynamic = self.identities.get(model, {}).get('dynamic', False)
        reasoning, transient = {s: [] for s in ('translate', 'review', 'judge')}, False
        blocked = False
        started = time.monotonic()
        for stage in (('translate',) if dynamic else ('translate', 'review', 'judge')):
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
                    resolved = body.get('_resolved_model') or resolve_model(name, {}, self.identities)
                    if resolved not in possible:
                        raise ToolError('验收响应的底层型号与候选不符。', 'mt_admission')
                    actual.add(resolved)
                    usage = body.get('usage') or {}
                    details = usage.get('completion_tokens_details') or {}
                    reasoning[stage].append(details.get('reasoning_tokens'))
                    if stage == 'translate':
                        text = ' '.join(value['translations'].values())
                        if set(re.findall(r'\d+', CASES[index][0])) != set(re.findall(r'\d+', text)):
                            passed = False
                            self.failures.append({'stage': stage, 'category': 'number_mismatch', 'case': index})
                        if 'cache' in CASES[index][0] and '缓存' not in text:
                            passed = False
                            self.failures.append({'stage': stage, 'category': 'terminology_mismatch', 'case': index})
                        samples[stage].append({'source': CASES[index][0], 'translations': value['translations']})
                    elif bool(value['issues']) != bool(index % 2):
                        passed = False
                        self.failures.append({'stage': stage, 'category': 'classification_error', 'case': index})
                    if stage == 'judge':
                        validate_judgment(value, data)
                        samples[stage].append({'source': CASES[index][0], 'translations': value['translations']})
                except Cancelled:
                    raise
                except AdmissionHTTPError as error:
                    blocked, passed, transient = True, False, error.retryable
                    break
                except TransportError as error:
                    self.failures.append({'stage': stage, 'category': error.category})
                    transient, passed = True, False
                    break
                except (ToolError, ValueError, TypeError, KeyError) as error:
                    self.failures.append({'stage': stage, 'category': getattr(error, 'code', 'invalid_response')})
                    passed = False
                    break
            if passed:
                roles.append(stage)
            if transient or blocked:
                break  # A refused candidate must not be sent again for the next role.
        # A separate established judge evaluates facts and naturalness in one call.
        checked_roles = [r for r in ('translate', 'judge') if r in roles]
        cases = [case for r in checked_roles for case in samples[r]]
        if cases and not transient and not blocked:
            accepted = self._judge(cases, actual | possible) if not model_set(self.judge, self.identities) & (actual | possible) else []
            for index, role in enumerate(checked_roles):
                if accepted[index * 4:(index + 1) * 4] != [True] * 4:
                    roles.remove(role)
                    self.failures.append({'stage': role, 'category': 'independent_judge_rejected'})
        non_thinking = {stage: len(values) == 4 and all(type(n) is int and n == 0 for n in values)
                        for stage, values in reasoning.items()}
        return {'roles': [] if transient or self.transient or blocked else roles, 'actual_models': sorted(possible if dynamic else actual),
                'observed_models': sorted(actual), 'failures': self.failures,
                'non_thinking': non_thinking, 'reasoning_tokens': reasoning,
                'retry_after': time.time() + 300 if transient or self.transient else None,
                'duration_ms': round((time.monotonic() - started) * 1000), 'suite': SUITE_VERSION}

    def _judge(self, cases, actual):
        check_provider_policy(self.config, self.judge)
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
                    resolve_model(body['model'], reply.headers, self.identities) not in actual and isinstance(value, dict) and
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
    fields = {key: provider.get(key) for key in ('chat', 'responses', 'anthropic', 'key', 'keys', 'headers', 'preset',
              '_plugin_fingerprint', '_admission_judge', '_judge_fingerprint')}
    fields['model_capabilities'] = provider.get('_models', {}).get(model)
    fields['identities'] = provider.get('_identities', {}).get(provider.get('id', '') + '/' + model)
    if fields['identities'] is not None:
        fields['identities'] = {k: v for k, v in fields['identities'].items() if k != 'admitted'}
    fields.update({k: provider[k] for k in ('replyHeaders', 'passHeaders', 'extraBody') if k in provider})
    return hashlib.sha256(json.dumps({'model': model, 'provider': fields,
        'suite': SUITE_VERSION, 'capabilities': CAPABILITIES, 'effort': ['none', 'low']},
        sort_keys=True).encode()).hexdigest()

"""Bounded synthetic model admission. No user transcript enters this suite."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from .common import Cancelled, ToolError
from .mt_quality import DEFAULT_CAPABILITIES, QUALITY_PROMPT_VERSION, make_stage_payload, parse_stage_response, parse_content, validate_judgment
from .mt_transport import ProcessTransport, TransportError
from .mt_identity import resolve_model, model_set
from .mt_channel_policy import ROLES, check_provider_policy, load_policy

SUITE_VERSION = 6
ADMISSION_TIMEOUT = 120  # Bounded total time for one synthetic request through the gateway.
SAFE_SERVICE_CODES = {'model_not_supported', 'model_not_found', 'invalid_api_key', 'insufficient_quota',
                      'rate_limit_exceeded', 'insufficient_credits', 'authentication_error', 'permission_denied',
                      'unsupported_parameter', 'invalid_request_error', 'client_not_supported'}


@dataclass(frozen=True)
class AdmissionCase:
    sentences: tuple[tuple[str, ...], ...]
    translations: tuple[str, ...]
    issue_id: str
    incorrect: str | None
    category: str

    @property
    def source(self):
        return ' '.join(fragment for sentence in self.sentences for fragment in sentence)


CASES = (
    AdmissionCase((('Do not disable the cache.',), ('It stores 12 results.',)),
                  ('不要禁用缓存。', '它存储 12 个结果。'), '0', None, 'negation'),
    AdmissionCase((('If the temperature exceeds 35 degrees,', 'stop the pump.'),),
                  ('如果温度超过 35 度，', '停止水泵。'), '0', '如果温度低于 35 度，', 'condition'),
    AdmissionCase((('The API returns 3 records, not 30.',),),
                  ('API 返回 3 条记录，而不是 30 条。',), '0', None, 'number'),
    AdmissionCase((('A cache stores computed results.',), ('The reader starts a lookup.',),
                   ('If the entry is present,', 'the reader reuses it.'), ('The page stays open.',),
                   ('Earlier values remain available.',), ('After a cache miss,', 'another calculation is required.'),
                   ('The counter records 12 misses.',), ('When the work finishes,', 'the display turns green.')),
                  ('缓存存储已计算的结果。', '读取程序发起查询。', '如果条目存在，', '读取程序就会复用它。',
                   '页面保持打开。', '先前的值仍然可用。', '缓存未命中后，', '需要重新计算。',
                   '计数器记录了 12 次未命中。', '工作完成后，', '显示器变为绿色。'), '7', '无需重新计算。', 'meaning'),
)


class AdmissionHTTPError(ToolError):
    def __init__(self, status):
        super().__init__('候选接口拒绝了验收请求。', 'mt_admission_http')
        self.retryable = status in {408, 429} or status >= 500


def current_role_members(registry):
    from .magpie import group_models
    groups = {g['id'] for g in registry.get('groups', [])}
    members = {}
    for role in ROLES:
        main = 'transcribe' + ('' if role == 'translate' else '-' + role)
        for group in (main, main + '-paid'):
            if group not in groups:
                continue
            for owner, name, provider in group_models(registry, 'group/' + group):
                member = owner + '/' + name
                if member in members and members[member][0] != role:
                    raise ToolError('同一网关成员已跨角色使用，未修改分组。', 'mt_admission')
                members[member] = (role, provider)
    return members


def select_admission_judge(config, member, catalog, *, policy=None, preferred=None):
    """Select an enabled, independent configured or established judge."""
    from .mt_catalog import read_registry
    policy = policy or load_policy(config)
    identities = catalog.get('identities', {})
    raw = read_registry(config.settings, catalog, policy=policy)
    candidates = list(policy['judges'])
    candidates.extend(m for m, (role, _) in current_role_members(raw).items() if role in {'review', 'judge'})
    if preferred in candidates:
        candidates.insert(0, preferred)
    enabled = {p['id']: p for p in raw['providers'] if not p.get('off') and p['id'] not in policy['excluded_providers']}
    possible = model_set(member, identities)
    for candidate in dict.fromkeys(candidates):
        owner, name = candidate.split('/', 1)
        if owner not in enabled or enabled[owner].get('noModels') or name in enabled[owner].get('modelsOff', []):
            continue
        try:
            if not possible & model_set(candidate, identities):
                return candidate
        except ValueError:
            continue  # An unverified dynamic judge cannot prove independence.
    raise ToolError('找不到与候选底层型号独立的验收评判者。', 'mt_admission')


def case_data(case):
    sentences = [{'id': str(index), 'text': ' '.join(parts)} for index, parts in enumerate(case.sentences)]
    fragments = []
    for sentence, parts in zip(sentences, case.sentences):
        for text in parts:
            fragments.append({'id': str(len(fragments)), 'sentence': sentence['id'], 'text': text})
    return {'sentences': sentences, 'fragments': fragments,
            'context': [], 'glossary': [{'source': 'cache', 'translation': '缓存', 'sentence': '0',
                                      'origin': 'manual', 'evidence': sentences[0]['text']}] if 'cache' in case.source else []}


def paired_sample(data, translations):
    return [{'source': sentence['text'], 'subtitles': [{'source': part['text'], 'translation': translations[part['id']]}
             for part in data['fragments'] if part['sentence'] == sentence['id']]} for sentence in data['sentences']]


class ModelEvaluator:
    def __init__(self, config, control, *, judge, transport=None, identities=None):
        self.config, self.control, self.judge = config, control, judge
        self.transport = transport or ProcessTransport()
        self.transient = False
        self.identities = identities or {}
        self.failures = []

    def request(self, model, stage, data):
        check_provider_policy(self.config, model)
        payload = make_stage_payload(stage, data, SimpleNamespace(model=model), capabilities=DEFAULT_CAPABILITIES)
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
            for index, case in enumerate(CASES):
                self.control.check_start()
                data = case_data(case)
                ids = [f['id'] for f in data['fragments']]
                sentence_ids = {item['id']: item['sentence'] for item in data['fragments']}
                target_sentence = sentence_ids[case.issue_id]
                if stage != 'translate':
                    data['translations'] = dict(zip(ids, case.translations, strict=True))
                    if case.incorrect is not None:
                        data['translations'][case.issue_id] = case.incorrect
                    if stage == 'judge':
                        data['issues'] = [{'id': case.issue_id, 'category': case.category,
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
                        sample = paired_sample(data, value['translations'])
                        if any(sorted(re.findall(r'\d+', sentence['source'])) != sorted(re.findall(r'\d+', ' '.join(
                                part['translation'] for part in sentence['subtitles']))) for sentence in sample):
                            passed = False
                            self.failures.append({'stage': stage, 'category': 'number_mismatch', 'case': index})
                        if any('cache' in sentence['source'].lower() and not any('缓存' in part['translation']
                               for part in sentence['subtitles']) for sentence in sample):
                            passed = False
                            self.failures.append({'stage': stage, 'category': 'terminology_mismatch', 'case': index})
                        samples[stage].append({'sentences': sample})
                    elif {sentence_ids[issue['id']] for issue in value['issues']} != (
                            {target_sentence} if case.incorrect is not None else set()):
                        passed = False
                        self.failures.append({'stage': stage, 'category': 'classification_error', 'case': index})
                    if stage == 'judge':
                        validate_judgment(value, data)
                        if (case.incorrect is not None and
                                re.sub(r'[\W_]', '', value['translations'][case.issue_id]) ==
                                re.sub(r'[\W_]', '', case.incorrect)):
                            passed = False
                            self.failures.append({'stage': stage, 'category': 'uncorrected_issue', 'case': index})
                        samples[stage].append({'sentences': paired_sample(data, value['translations'])})
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
                if accepted[index * len(CASES):(index + 1) * len(CASES)] != [True] * len(CASES):
                    roles.remove(role)
                    self.failures.append({'stage': role, 'category': 'independent_judge_rejected'})
        non_thinking = {stage: len(values) == len(CASES) and all(type(n) is int and n == 0 for n in values)
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
                {'role': 'system', 'content': '逐句核对 source 与 subtitles 的对应关系、完整性和自然度；特别检查数字、条件、否定、术语、跨片重复和跨句错位。'
                    f'输入顶层数组有 {len(cases)} 个样例，每个样例按编号连读其全部片段，与该样例原文一起评价一次。'
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
        'suite': SUITE_VERSION, 'prompt': QUALITY_PROMPT_VERSION, 'capabilities': DEFAULT_CAPABILITIES, 'effort': ['none', 'low']},
        sort_keys=True).encode()).hexdigest()

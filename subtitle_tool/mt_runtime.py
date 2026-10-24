"""Bounded MT retries, shared recovery and OpenRouter free-quota accounting."""
from __future__ import annotations

import json as jsonlib
import random
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone

from .api import mt_pacer, retry_after_seconds
from .common import ToolError
from .mt_backend import normalize_mt_url
from .mt_transport import HTTPReply, ProcessTransport, TransportError


TRANSIENT_TYPES = {'timeout', 'provider_unavailable', 'provider_overloaded', 'server', 'rate_limit_exceeded'}
PERMANENT_TYPES = {
    'context_length_exceeded', 'max_tokens_exceeded', 'token_limit_exceeded', 'string_too_long',
    'authentication', 'permission_denied', 'payment_required', 'invalid_request', 'invalid_prompt',
    'not_found', 'precondition_failed', 'payload_too_large', 'unprocessable',
    'content_policy_violation', 'refusal', 'invalid_image', 'image_too_large', 'image_too_small',
    'unsupported_image_format', 'image_not_found', 'image_download_failed',
}
RUNTIME_CATEGORIES = TRANSIENT_TYPES | PERMANENT_TYPES | {
    'network', 'deadline', 'transport', 'response_too_large', 'http_transient', 'http_permanent',
    'service_unknown', 'response_structure', 'shared_wait', 'quota_observed',
    'quota_refresh_failed', 'quota_unknown', 'quota_exhausted', 'drain_discard', 'retry_after',
    'cancelled', 'drain_stopped', 'format_exhausted', 'transient_exhausted', 'response_received', 'group_committed',
}


class ServiceError(ToolError):
    def __init__(self, category, status=None, retryable=False, *, code=None, provider='openrouter', quota_source=None):
        self.category, self.status, self.retryable = category, status, retryable
        self.quota_source = quota_source
        hint = {
            'deadline': 'OpenRouter 请求总时限耗尽，进度已保存；可显式恢复。',
            'network': 'OpenRouter 网络连接或读取失败，进度已保存。',
            'transport': 'OpenRouter 网络子进程异常，进度已保存。',
            'response_too_large': 'OpenRouter 响应超过 4 MiB 上限，进度已保存。',
            'retry_after': 'OpenRouter 要求等待超过 300 秒，已结束本轮并保存进度。',
            'quota_exhausted': ('OpenRouter 本地免费可用余量归零，已保存进度；请稍后显式 --resume。'
                                if quota_source == 'local_ledger' else
                                'OpenRouter 服务端观测免费额度不足，已保存进度；请稍后显式 --resume。'),
            'quota_unknown': '无法确认 OpenRouter 免费额度，已保存进度；请稍后显式 --resume。',
            'authentication': 'OpenRouter 翻译凭据被拒绝，进度已保存。',
            'permission_denied': 'OpenRouter 翻译权限被拒绝，进度已保存。',
            'payment_required': 'OpenRouter 翻译账户余额不足，进度已保存。',
            'not_found': 'OpenRouter 未找到指定服务或模型，进度已保存。',
            'invalid_request': 'OpenRouter 翻译请求参数被拒绝，进度已保存。',
            'invalid_prompt': 'OpenRouter 翻译输入被拒绝，进度已保存。',
            'content_policy_violation': 'OpenRouter 内容政策拒绝了翻译请求，进度已保存。',
            'refusal': 'OpenRouter 翻译请求被模型拒绝，进度已保存。',
        }.get(category)
        if hint is None:
            hint = {400: 'OpenRouter 请求参数被拒绝', 401: 'OpenRouter 翻译凭据被拒绝',
                    402: 'OpenRouter 账户余额或额度不足', 403: 'OpenRouter 请求权限或内容被拒绝',
                    404: 'OpenRouter 未找到可用服务或模型', 408: 'OpenRouter 请求超时',
                    429: 'OpenRouter 翻译请求限流', 503: 'OpenRouter 服务暂不可用'}.get(
                        status, 'OpenRouter 翻译服务返回错误') + '；进度已保存。'
        if provider == 'bailian':
            hint = hint.replace('OpenRouter', '百炼')
        super().__init__(hint, code or (f'mt_http_{status}' if status and not 200 <= status < 300
                                      else 'mt_service_error'))


def service_failure(status, body, *, free_router=False):
    errors = []
    business = False
    if isinstance(body, dict):
        if 'error' in body:
            business = True
            errors.append(body['error'])
        choices = body.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and (choice.get('finish_reason') == 'content_filter' or
                        isinstance(choice.get('message'), dict) and choice['message'].get('refusal')):
                    return ServiceError('content_policy_violation', status, code='mt_content_refused')
                if isinstance(choice, dict) and ('error' in choice or choice.get('finish_reason') == 'error'):
                    business = True
                    errors.append(choice.get('error'))
    kinds = []
    malformed_type = False
    for error in errors:
        metadata = error.get('metadata') if isinstance(error, dict) else None
        kind = metadata.get('error_type') if isinstance(metadata, dict) else None
        if isinstance(metadata, dict) and 'error_type' in metadata and not isinstance(kind, str):
            malformed_type = True
        kinds.append(kind if isinstance(kind, str) else None)
    for kind in kinds:
        if kind in PERMANENT_TYPES and not (free_router and kind == 'not_found'):
            return ServiceError(kind, status)
    if status in {400, 401, 402, 403, 405, 413, 415, 422} or status == 404 and not free_router:
        return ServiceError('http_permanent', status)
    # Explicit but unknown types and untyped 2xx business failures are never guessed from messages/codes.
    retryable_types = TRANSIENT_TYPES | {'not_found'} if free_router else TRANSIENT_TYPES
    if malformed_type or any(kind is not None and kind not in retryable_types for kind in kinds):
        return ServiceError('service_unknown', status)
    if free_router and 'not_found' in kinds:
        # Dynamic free routing gets a bounded retry, without masking an untyped companion error.
        if not all(kind in retryable_types for kind in kinds):
            return ServiceError('service_unknown', status)
        retryable = 200 <= status < 300 or status in {404, 408, 429} or 500 <= status <= 599
        return ServiceError('not_found', status, retryable)
    if kinds and all(kind in retryable_types for kind in kinds):
        return ServiceError(kinds[0], status, True)
    if not 200 <= status < 300:
        retryable = status in {408, 429} or 500 <= status <= 599 or status == 404 and free_router
        return ServiceError('http_transient' if retryable else 'http_permanent', status, retryable)
    if business:
        return ServiceError('service_unknown', status)
    return None


def bailian_failure(status, body):
    content = service_failure(200, body)
    if content is not None and content.category == 'content_policy_violation':
        return ServiceError('content_policy_violation', status, code='mt_content_refused', provider='bailian')
    error = body.get('error') if isinstance(body, dict) else None
    code = (error.get('code', error.get('type')) if isinstance(error, dict) else None)
    if code is None and isinstance(body, dict):
        code = body.get('code')
    permanent = {'InvalidApiKey': 'authentication', 'invalid_api_key': 'authentication',
        'AccessDenied': 'permission_denied', 'access_denied': 'permission_denied',
        'Arrearage': 'payment_required', 'arrearage': 'payment_required',
        'InvalidParameter': 'invalid_request', 'InvalidParameterValue': 'invalid_request',
        'invalid_request_error': 'invalid_request', 'InternalError.Algo.InvalidParameter': 'invalid_request',
        'DataInspectionFailed': 'content_policy_violation', 'data_inspection_failed': 'content_policy_violation',
        'InputDataInspectionFailed': 'content_policy_violation', 'OutputDataInspectionFailed': 'content_policy_violation'}
    transient = {'Throttling.RateQuota', 'Throttling.BurstRate', 'Throttling.AllocationQuota',
        'Throttling.ServiceOverloaded', 'Throttling.ResourceExhausted', 'LimitRequests', 'limit_requests',
        'limit_burst_rate', 'insufficient_quota', 'rate_limit_exceeded', 'InternalError', 'internal_error',
        'InternalError.Algo', 'InternalError.Timeout', 'InternalError.Configuration',
        'InternalError.DataInspection', 'InternalError.TranslationFailed', 'SystemError', 'ModelServiceFailed',
        'RequestTimeOut', 'RequestTimeout', 'ServiceUnavailable'}
    if isinstance(code, str) and code in permanent:
        return ServiceError(permanent[code], status, provider='bailian')
    if status in {400, 401, 402, 403, 404, 413, 422}:
        return ServiceError('http_permanent', status, provider='bailian')
    if isinstance(code, str) and code in transient:
        rate_codes = {'LimitRequests', 'limit_requests', 'limit_burst_rate', 'insufficient_quota', 'rate_limit_exceeded'}
        category = ('rate_limit_exceeded' if code.startswith('Throttling') or code in rate_codes else
                    'timeout' if code in {'InternalError.Timeout', 'RequestTimeOut', 'RequestTimeout'} else 'server')
        return ServiceError(category, status, True, provider='bailian')
    if status in {408, 429} or status >= 500:
        return ServiceError('http_transient', status, True, provider='bailian')
    if (not 200 <= status < 300 or error is not None or
            code is not None and (not isinstance(code, (str, int)) or code not in {'Success', '200', 200})):
        return ServiceError('service_unknown', status, provider='bailian')
    return None


@dataclass(frozen=True)
class Ticket:
    generation: int
    probe: bool
    pacer_generation: int


class RecoveryCoordinator:
    def __init__(self, *, clock=time.monotonic, jitter=random.random, wait=None):
        self.clock, self.jitter, self.wait = clock, jitter, wait or (lambda control, seconds: control.wait(seconds))
        self.lock = threading.Lock()
        self.generation = 0
        self.failures = 0
        self.cooldown_until = 0.0
        self.recovering = False
        self.probe_active = False
        self.deferred_until = 0.0

    def acquire(self, control, pacer, *, progress=lambda *args: None, on_event=lambda event: None,
                prepare=lambda: None, admit=lambda: True):
        shared_wait = pacing_wait = 0.0
        quota_notice = service_notice = False
        ticket = None
        try:
            while True:
                control.check_start()
                with self.lock:
                    now = self.clock()
                    if self.deferred_until > now:
                        raise ServiceError('retry_after', code='mt_retry_after')
                    recovery_delay = max(0.0, self.cooldown_until - now)
                    if self.recovering and self.probe_active:
                        recovery_delay = max(recovery_delay, .1)
                    pacing_delay = max(0.0, pacer.ready_at() - now)
                    quota_delay = max(0.0, pacer.quota_ready_at() - now)
                    delay = max(recovery_delay, pacing_delay)
                if delay <= 0:
                    # Quota I/O may take 15s. Do it before consuming RPM/probe admission,
                    # then recheck all shared deadlines under the gate lock.
                    prepare()
                    control.check_start()
                    with self.lock:
                        now = self.clock()
                        if (self.deferred_until > now or self.cooldown_until > now
                                or self.recovering and self.probe_active):
                            continue
                        pacer_generation = pacer.try_start(admit)
                        if pacer_generation is not None:
                            probe = self.recovering
                            if probe:
                                self.probe_active = True
                            ticket = Ticket(self.generation, probe, pacer_generation)
                            return ticket
                    continue
                if quota_delay >= 2 and quota_delay >= delay and not quota_notice:
                    progress('mt_quota_wait', quota_delay, pacer.requests_per_window)
                    quota_notice = True
                if recovery_delay >= 2 and not service_notice:
                    progress('mt_service_wait', recovery_delay, None)
                    service_notice = True
                started = self.clock()
                try:
                    self.wait(control, min(delay, .1))
                finally:
                    elapsed = max(0.0, self.clock() - started)
                    shared_wait += min(elapsed, recovery_delay)
                    pacing_wait += max(0.0, min(elapsed, pacing_delay) - min(elapsed, recovery_delay))
        finally:
            try:
                if shared_wait or pacing_wait:
                    on_event({'kind': 'runtime_wait', 'category': 'shared_wait',
                              'shared_wait_ms': round(shared_wait * 1000), 'pacing_wait_ms': round(pacing_wait * 1000)})
            except BaseException:
                if ticket is not None:
                    self.abandon(ticket)
                raise

    def failure(self, ticket, *, retry_after=None):
        with self.lock:
            now = self.clock()
            if ticket.generation == self.generation:
                self.generation += 1
                self.failures += 1
                delay = min(60, 15 * 2 ** min(self.failures - 1, 2)) + self.jitter()
                self.cooldown_until = max(self.cooldown_until, now + delay)
                self.recovering = True
                self.probe_active = False
            if retry_after is not None:
                until = now + retry_after
                if (until > self.cooldown_until and ticket.generation < self.generation
                        and (self.probe_active or not self.recovering)):
                    # A late peer supplied a newer deadline: the already-started probe cannot clear it.
                    self.generation += 1
                    self.probe_active = False
                    self.recovering = True
                self.cooldown_until = max(self.cooldown_until, until)
                if retry_after > 300:
                    self.deferred_until = max(self.deferred_until, until)
            return max(0.0, self.cooldown_until - now)

    def success(self, ticket):
        with self.lock:
            if ticket.generation == self.generation and ticket.probe:
                self.recovering = False
                self.probe_active = False
                self.failures = 0

    def abandon(self, ticket):
        with self.lock:
            if ticket.generation == self.generation and ticket.probe:
                self.probe_active = False


class FreeQuota:
    """Process-local conservative ledger; all models using the same account share it."""
    def __init__(self, *, clock=time.monotonic, utcnow=lambda: datetime.now(timezone.utc), wait=None):
        self.clock, self.utcnow = clock, utcnow
        self.wait = wait or (lambda control, seconds: control.wait(seconds))
        self.condition = threading.Condition()
        self.refreshing = False
        self.last_attempt = None
        self.day = None
        self.remaining = 0
        self.inflight = 0
        self.reservations = 0
        self.trusted = weakref.WeakSet()
        self.observation = None
        self.observed_monotonic = None

    @staticmethod
    def _read(reply: HTTPReply):
        if reply.status != 200:
            raise ValueError()
        body = jsonlib.loads(reply.body)
        value = body['data']['free_model_daily_requests']
        if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] < 0
                                              for k in ('used', 'limit', 'remaining')):
            raise ValueError()
        return {k: value[k] for k in ('used', 'limit', 'remaining')}

    def ensure(self, control, fetch, on_event=lambda event: None):
        while True:
            control.check_start()
            with self.condition:
                day, now = self.utcnow().date(), self.clock()
                if self.day == day and self.observed_monotonic is not None and control.created <= self.observed_monotonic:
                    self.trusted.add(control)
                trusted = self.day == day and control in self.trusted
                if trusted and self.remaining <= 0:
                    raise self.exhaustion()
                if self.refreshing:
                    if trusted:
                        return
                    self.condition.wait(.1)
                    continue
                due = self.last_attempt is None or now - self.last_attempt >= 30
                if not due:
                    if trusted:
                        return
                    # An untrusted run must obtain its own observation, without exceeding query rate.
                    if self.observation is not None:
                        delay = min(.1, self.last_attempt + 30 - now)
                    else:
                        raise ServiceError('quota_unknown', code='mt_quota_unknown')
                else:
                    self.refreshing = True
                    self.last_attempt = now
                    sent, active = self.reservations, self.inflight
                    break
            self.wait(control, delay)
        value = None
        try:
            value = self._read(fetch())
        except (TransportError, ValueError, KeyError, TypeError, RecursionError):
            pass
        finally:
            with self.condition:
                observed = self.utcnow()
                if value is not None and observed.date() == day:
                    remote = min(value['remaining'], max(0, value['limit'] - value['used']))
                    available = max(0, remote - active - (self.reservations - sent))
                    self.remaining = min(self.remaining, available) if self.day == day else available
                    self.day = day
                    self.trusted.add(control)
                    self.observed_monotonic = self.clock()
                    self.observation = {**value, 'observed_at': observed.isoformat()}
                    event = {'kind': 'runtime_quota', 'category': 'quota_observed', **self.observation,
                             'local_remaining': self.remaining}
                else:
                    event = {'kind': 'runtime_quota', 'category': 'quota_refresh_failed'}
                self.refreshing = False
                self.condition.notify_all()
            on_event(event)
        control.check_start()
        with self.condition:
            if self.day != self.utcnow().date() or control not in self.trusted:
                raise ServiceError('quota_unknown', code='mt_quota_unknown')
            if self.remaining <= 0:
                raise self.exhaustion()

    def exhaustion(self):
        observed = self.observation or {}
        remote_empty = (observed.get('remaining') == 0 or
                        'limit' in observed and observed['used'] >= observed['limit'])
        return ServiceError('quota_exhausted', code='mt_quota_exhausted',
                            quota_source='server_observation' if remote_empty else 'local_ledger')

    def try_reserve(self, control):
        """No I/O or waits: combine quota admission with the shared RPM gate."""
        with self.condition:
            control.check_start()
            if (self.day != self.utcnow().date() or control not in self.trusted or
                    not self.refreshing and (self.last_attempt is None or self.clock() - self.last_attempt >= 30)):
                return False
            if self.remaining <= 0:
                raise self.exhaustion()
            self.remaining -= 1
            self.inflight += 1
            self.reservations += 1
            return True

    def complete(self):
        with self.condition:
            self.inflight -= 1


_RECOVERY = {}
_QUOTAS = {}
_REGISTRY_LOCK = threading.Lock()


def runtime_resources(backend):
    identity = (backend.provider, backend.base_url, backend.key_fingerprint, backend.model)
    account = (normalize_mt_url(backend.base_url), backend.key_fingerprint)
    with _REGISTRY_LOCK:
        recovery = _RECOVERY.setdefault(identity, RecoveryCoordinator())
        quota = _QUOTAS.setdefault(account, FreeQuota())
        return recovery, quota


def reset_quota_for_resume(backend):
    """Only explicit resume resets the observation; backend cooldown/RPM remain intact."""
    account = (normalize_mt_url(backend.base_url), backend.key_fingerprint)
    with _REGISTRY_LOCK:
        ledger = _QUOTAS.get(account)
        if ledger is not None:
            with ledger.condition:
                if ledger.inflight or ledger.refreshing:
                    raise ToolError('翻译请求仍在执行，不能同时恢复任务。', 'instance_busy')
            _QUOTAS.pop(account, None)


class BoundedMTHttp:
    safe_errors = True

    def __init__(self, backend, control, count=lambda name: None, progress=lambda *args: None, *,
                 rpm=0, transport=None, pacer=None, recovery=None, quota=None, on_event=lambda event: None,
                 stop_on_failure=True):
        shared_recovery, shared_quota = runtime_resources(backend)
        self.backend, self.control, self.cancel = backend, control, control.cancel
        self.count, self.progress, self.on_event = count, progress, on_event
        self.transport = transport if transport is not None else ProcessTransport()
        self.pacer = pacer if pacer is not None else mt_pacer(backend, rpm=rpm)
        self.recovery = recovery if recovery is not None else shared_recovery
        self.quota = (quota if quota is not None else shared_quota) if backend.free_quota else None
        self.stop_on_failure = stop_on_failure

    def ensure_result(self):
        try:
            self.control.check_inflight()
        except ToolError:
            self.on_event({'kind': 'runtime_drain', 'category': 'drain_discard', 'discarded': 1})
            raise

    def json(self, method, url, *, name='mt', headers=None, json=None):
        def fetch():
            self.control.check_start()
            self.count('mt_quota_query')
            return self.transport.request('GET', self.backend.base_url + '/key', headers=headers or {},
                                          control=self.control, timeout=15.0, drain=False)
        for attempt in range(1, 5):
            self.control.check_start()
            ticket = None
            reserved = False
            try:
                if self.quota is not None:
                    self.quota.ensure(self.control, fetch, self.on_event)
                def prepare():
                    if self.quota is not None:
                        self.quota.ensure(self.control, fetch, self.on_event)
                def admit():
                    nonlocal reserved
                    if self.quota is not None:
                        reserved = self.quota.try_reserve(self.control)
                        return reserved
                    return True
                ticket = self.recovery.acquire(self.control, self.pacer, progress=self.progress,
                                               on_event=self.on_event, prepare=prepare, admit=admit)
                self.count(name)
                self.control.check_start()
                started = time.monotonic()
                retry_after = None
                body = None
                try:
                    reply = self.transport.request(method, url, headers=headers or {}, json=json,
                                                   control=self.control, timeout=120.0)
                    status = reply.status
                    if 200 <= status < 300:
                        self.count('mt_success')
                    retry_after = retry_after_seconds(reply.retry_after)
                    try:
                        body = jsonlib.loads(reply.body)
                    except (ValueError, RecursionError):
                        pass
                    error = (bailian_failure(status, body) if self.backend.provider == 'bailian' else
                             service_failure(status, body, free_router=self.backend.free_router))
                except TransportError as failure:
                    status = None
                    error = ServiceError(failure.category, retryable=failure.retryable,
                        code='mt_request_deadline' if failure.category == 'deadline' else failure.code,
                        provider='bailian' if self.backend.provider == 'bailian' else 'openrouter')
                except ToolError:
                    self.on_event({'kind': 'runtime_drain', 'category': 'drain_discard', 'discarded': 1})
                    self.on_event({'kind': 'runtime_request', 'category': 'cancelled' if self.cancel.is_set() else 'drain_stopped',
                        'attempt': attempt, 'duration_ms': round((time.monotonic() - started) * 1000),
                        'retryable': False, 'deadline_terminated': False})
                    raise
                self.on_event({'kind': 'runtime_request', 'category': error.category if error else 'response_received',
                    'attempt': attempt, 'status': status, 'duration_ms': round((time.monotonic() - started) * 1000),
                    'actual_model': body.get('model') if isinstance(body, dict) else None,
                    'retryable': bool(error and error.retryable), 'deadline_terminated': bool(error and error.category == 'deadline')})
                if error is None:
                    self.recovery.success(ticket)
                    changed = self.pacer.success(ticket.pacer_generation)
                    if changed:
                        self.progress('mt_recovery', *changed)
                    self.ensure_result()
                    if not isinstance(body, dict):
                        raise ToolError('翻译响应不是合法 JSON 对象；保留格式修复规则。', 'response_schema')
                    return body
                self.pacer.failure(ticket.pacer_generation)
                if error.retryable:
                    delay = self.recovery.failure(ticket, retry_after=retry_after)
                    if status == 429 or error.category == 'rate_limit_exceeded':
                        self.count('mt_429')
                        added, interval = self.pacer.throttle(delay, ticket.pacer_generation)
                        self.progress('mt_cooldown', added, interval)
                if retry_after is not None and retry_after > 300 and error.retryable:
                    raise ServiceError('retry_after', status, code='mt_retry_after',
                                       provider='bailian' if self.backend.provider == 'bailian' else 'openrouter')
                if not error.retryable or attempt == 4:
                    raise error
                self.progress('retry', attempt, 4)
            except ToolError as error:
                error.attempts = attempt if ticket is not None else 0
                if self.stop_on_failure and error.code not in {'response_schema', 'cancelled'}:
                    self.control.stop(error)
                if isinstance(error, ServiceError) and error.category in {'quota_unknown', 'quota_exhausted'}:
                    self.on_event({'kind': 'runtime_quota', 'category': error.category,
                                   'quota_source': error.quota_source})
                raise
            finally:
                if ticket is not None:
                    self.recovery.abandon(ticket)
                if reserved:
                    self.quota.complete()

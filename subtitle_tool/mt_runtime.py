"""Provider error classification and shared OpenRouter free-quota accounting."""
from __future__ import annotations

import json as jsonlib
import threading
import time
import weakref
from datetime import datetime, timezone

from .common import ToolError
from .mt_backend import normalize_mt_url
from .mt_transport import HTTPReply


TRANSIENT_TYPES = {'timeout', 'provider_unavailable', 'provider_overloaded', 'server', 'rate_limit_exceeded'}
PERMANENT_TYPES = {
    'context_length_exceeded', 'max_tokens_exceeded', 'token_limit_exceeded', 'string_too_long',
    'authentication', 'permission_denied', 'payment_required', 'invalid_request', 'invalid_prompt',
    'not_found', 'precondition_failed', 'payload_too_large', 'unprocessable',
    'content_policy_violation', 'refusal', 'invalid_image', 'image_too_large', 'image_too_small',
    'unsupported_image_format', 'image_not_found', 'image_download_failed',
}
class ServiceError(ToolError):
    def __init__(self, category, status=None, retryable=False, *, code=None, quota_source=None):
        self.category, self.status, self.retryable = category, status, retryable
        self.quota_source = quota_source
        super().__init__("翻译服务请求失败，进度已保存。", code or (f'mt_http_{status}' if status and not 200 <= status < 300
                                      else 'mt_service_error'))


def _service_errors(body):
    """Extract independent errors and content-free shape counts from one envelope."""
    errors = []
    shape = {'top_level_error': False, 'choice_error_count': 0, 'detail_free_error_finish_count': 0,
             'content_filter_count': 0, 'message_refusal_count': 0,
             'error_type_malformed_count': 0}
    if isinstance(body, dict):
        if 'error' in body:
            shape['top_level_error'] = True
            errors.append(body['error'])
        choices = body.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get('finish_reason') == 'content_filter':
                    shape['content_filter_count'] += 1
                if isinstance(choice.get('message'), dict) and choice['message'].get('refusal'):
                    shape['message_refusal_count'] += 1
                if 'error' in choice:
                    shape['choice_error_count'] += 1
                    errors.append(choice['error'])
                elif choice.get('finish_reason') == 'error':
                    shape['detail_free_error_finish_count'] += 1
    kinds = []
    for error in errors:
        metadata = error.get('metadata') if isinstance(error, dict) else None
        kind = metadata.get('error_type') if isinstance(metadata, dict) else None
        if isinstance(metadata, dict) and 'error_type' in metadata and not isinstance(kind, str):
            shape['error_type_malformed_count'] += 1
        kinds.append(kind if isinstance(kind, str) else None)
    if not shape['top_level_error']:
        # A top-level error explains its finish markers, but never an explicit choice.error.
        kinds.extend([None] * shape['detail_free_error_finish_count'])
    return kinds, shape, bool(shape['content_filter_count'] or shape['message_refusal_count'])


def service_failure(status, body, *, free_router=False):
    kinds, shape, content_refused = _service_errors(body)
    def failure(category, retryable=False, *, code=None):
        return ServiceError(category, status, retryable, code=code)
    if content_refused:
        return failure('content_policy_violation', code='mt_content_refused')
    for kind in kinds:
        if kind in PERMANENT_TYPES and not (free_router and kind == 'not_found'):
            return failure(kind)
    if status in {400, 401, 402, 403, 405, 413, 415, 422} or status == 404 and not free_router:
        return failure('http_permanent')
    if (shape['detail_free_error_finish_count'] and not shape['top_level_error'] and
            not shape['choice_error_count']):
        retryable = (200 <= status < 300 or status in {408, 429} or 500 <= status <= 599 or
                     status == 404 and free_router)
        return failure('generation_error', retryable)
    retryable_types = TRANSIENT_TYPES | {'not_found'} if free_router else TRANSIENT_TYPES
    untyped_choice = any(kind is None for kind in kinds) and (
        shape['choice_error_count'] or shape['detail_free_error_finish_count'] and not shape['top_level_error'])
    if (shape['error_type_malformed_count'] or untyped_choice or
            any(kind is not None and kind not in retryable_types for kind in kinds)):
        return failure('service_unknown')
    if free_router and 'not_found' in kinds:
        retryable = 200 <= status < 300 or status in {404, 408, 429} or 500 <= status <= 599
        return failure('not_found', retryable)
    if kinds and all(kind in retryable_types for kind in kinds):
        return failure(kinds[0], True)
    if not 200 <= status < 300:
        retryable = status in {408, 429} or 500 <= status <= 599 or status == 404 and free_router
        return failure('http_transient' if retryable else 'http_permanent', retryable)
    if kinds:
        return failure('service_unknown')
    return None


def bailian_failure(status, body):
    content = service_failure(200, body)
    if content is not None and content.category == 'content_policy_violation':
        return ServiceError('content_policy_violation', status, code='mt_content_refused')
    error = body.get('error') if isinstance(body, dict) else None
    code = (error.get('code', error.get('type')) if isinstance(error, dict) else None)
    if code is None and isinstance(body, dict):
        code = body.get('code')
    # This exact status/code pair is the server's free-tier stop signal.
    # Other 403/429 responses are not evidence that a model's quota is empty.
    if status == 403 and code == 'AllocationQuota.FreeTierOnly':
        return ServiceError('quota_exhausted', status, code='mt_quota_exhausted',
                            quota_source='free_tier_only')
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
        return ServiceError(permanent[code], status)
    if status in {400, 401, 402, 403, 404, 413, 422}:
        return ServiceError('http_permanent', status)
    if isinstance(code, str) and code in transient:
        rate_codes = {'LimitRequests', 'limit_requests', 'limit_burst_rate', 'insufficient_quota', 'rate_limit_exceeded'}
        category = ('rate_limit_exceeded' if code.startswith('Throttling') or code in rate_codes else
                    'timeout' if code in {'InternalError.Timeout', 'RequestTimeOut', 'RequestTimeout'} else 'server')
        return ServiceError(category, status, True)
    if status in {408, 429} or status >= 500:
        return ServiceError('http_transient', status, True)
    if (not 200 <= status < 300 or error is not None or
            code is not None and (not isinstance(code, (str, int)) or code not in {'Success', '200', 200})):
        return ServiceError('service_unknown', status)
    return None


class FreeQuota:
    """Process-local conservative ledger; all models using the same account share it."""
    def __init__(self, *, clock=time.monotonic, utcnow=lambda: datetime.now(timezone.utc)):
        self.clock, self.utcnow = clock, utcnow
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

    def _finish_refresh(self, control, value, day, sent, active):
        """Commit one observation and account for reservations made during its query."""
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
        return event

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

_QUOTAS = {}
_REGISTRY_LOCK = threading.Lock()


def quota_for(backend):
    account = (normalize_mt_url(backend.base_url), backend.key_fingerprint)
    with _REGISTRY_LOCK:
        return _QUOTAS.setdefault(account, FreeQuota())


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

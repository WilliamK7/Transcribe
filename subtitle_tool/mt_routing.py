"""Bounded gateway repair and paid validation fallback with exact caches."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .common import ToolError
from .mt_backend import MTBackend


STAGE_TIMEOUT = 180.0
_SCOPES = frozenset({'entry', 'model', 'credential', 'endpoint', 'paid_account'})
_CATEGORIES = frozenset({
    'timeout', 'provider_unavailable', 'provider_overloaded', 'server', 'rate_limit_exceeded',
    'context_length_exceeded', 'max_tokens_exceeded', 'token_limit_exceeded', 'string_too_long',
    'authentication', 'permission_denied', 'payment_required', 'invalid_request', 'invalid_prompt',
    'not_found', 'precondition_failed', 'payload_too_large', 'unprocessable',
    'content_policy_violation', 'refusal', 'invalid_image', 'image_too_large', 'image_too_small',
    'unsupported_image_format', 'image_not_found', 'image_download_failed', 'network', 'deadline',
    'transport', 'response_too_large', 'http_transient', 'http_permanent', 'service_unknown',
    'generation_error', 'response_structure', 'format_exhausted', 'output_truncated', 'cancelled', 'local_error',
    'model_not_independent',
})


@dataclass(frozen=True)
class RouteEntry:
    id: str
    backend: MTBackend
    tier: str


@dataclass(frozen=True)
class RouteResult:
    value: object
    entry: RouteEntry
    cached: bool
    attempts: int


class RouteFailure(ToolError):
    def __init__(self, category, *, scope='entry', repairable=False, fallback_allowed=False):
        super().__init__('翻译网关请求失败。', 'mt_route_failure')
        self.category = category if category in _CATEGORIES else 'service_unknown'
        self.scope = scope if scope in _SCOPES else 'entry'
        self.repairable = repairable
        self.fallback_allowed = fallback_allowed


class RouteExhausted(ToolError):
    def __init__(self, *, deadline=False, category='', request_id=''):
        if category == 'output_truncated':
            message, code = ('翻译响应达到输出上限而被截断，进度已保存；可用 --resume 重试。',
                             'mt_output_truncated')
        elif deadline:
            message, code = '翻译阶段已超过时限，进度已保存。', 'mt_route_deadline'
        else:
            reason = f'（{category}）' if category in _CATEGORIES else ''
            message, code = f'翻译网关请求失败{reason}，进度已保存。', 'mt_route_exhausted'
        super().__init__(message, code, request_id)


class MagpieRoute:
    def __init__(self, entries, *, control, on_event, clock=time.monotonic):
        self.entries = tuple(entries)
        self.control = control
        self.on_event, self.clock = on_event, clock

    def cached_result(self, cache, *, deadline):
        """Reuse completed work in route order."""
        for entry in self.entries:
            self.control.check_start()
            if self.clock() >= deadline:
                raise RouteExhausted(deadline=True)
            hit = cache(entry)
            if hit is not None:
                self.control.check_inflight()
                if self.clock() >= deadline:
                    raise RouteExhausted(deadline=True)
                return RouteResult(hit, entry, True, 0)
        return None

    def run(self, invoke, *, validate, deadline):
        remaining = max(0.0, deadline - self.clock())
        primary_deadline = deadline - min(STAGE_TIMEOUT / 3, remaining / 3)
        self.control.check_start()
        attempts = 0
        for index, entry in enumerate(self.entries):
            entry_deadline = primary_deadline if index == 0 else deadline
            for attempt in (1, 2):
                self.control.check_start()
                if self.clock() >= deadline:
                    raise RouteExhausted(deadline=True)
                if index == 0 and self.clock() >= entry_deadline:
                    break
                try:
                    validate(entry)
                    self.control.check_start()
                    remaining = entry_deadline - self.clock()
                    if remaining <= 0:
                        raise RouteFailure('deadline', fallback_allowed=True)
                    attempts += 1
                    self.on_event({'kind': 'route_attempt', 'entry': entry.id, 'tier': entry.tier, 'attempt': attempt})
                    value = invoke(entry, remaining, attempt)
                except RouteFailure as failure:
                    self.control.check_start()
                    self.on_event({'kind': 'route_failure', 'entry': entry.id, 'tier': entry.tier,
                                   'category': failure.category, 'scope': failure.scope, 'attempt': attempt})
                    # Validation and a bounded request deadline may switch pools.
                    # Network, HTTP, refusal and uncertain response shapes do not.
                    if attempt == 1 and failure.repairable:
                        continue
                    if index == 0 and failure.fallback_allowed:
                        break
                    raise RouteExhausted(deadline=failure.category in {'deadline', 'timeout'},
                                         category=failure.category, request_id=failure.request_id) from None
                self.control.check_inflight()
                if self.clock() >= deadline:
                    raise RouteExhausted(deadline=True)
                return RouteResult(value, entry, False, attempts)

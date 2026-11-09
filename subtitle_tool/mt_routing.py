"""Gateway dispatch with one locally authorized format repair and exact caches."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .common import ToolError
from .mt_backend import MTBackend


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
})


@dataclass(frozen=True)
class RouteEntry:
    id: str
    backend: MTBackend
    tier: str

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.id):
            raise ValueError('Invalid route id')
        if not isinstance(self.backend, MTBackend) or self.tier not in {'free', 'paid'}:
            raise ValueError('Invalid route backend or tier')


@dataclass(frozen=True)
class RouteResult:
    value: object
    entry: RouteEntry
    cached: bool
    attempts: int


class RouteFailure(ToolError):
    def __init__(self, category, *, scope='entry', repairable=False):
        super().__init__('翻译网关请求失败。', 'mt_route_failure')
        self.category = category if category in _CATEGORIES else 'service_unknown'
        self.scope = scope if scope in _SCOPES else 'entry'
        self.repairable = repairable


class RouteExhausted(ToolError):
    def __init__(self, *, deadline=False):
        super().__init__('翻译阶段已超过时限，进度已保存。' if deadline else
                         '没有可用的翻译候选，进度已保存。',
                         'mt_route_deadline' if deadline else 'mt_route_exhausted')


def cached_route_result(entries, control, clock, cache, *, prefer_other_than=None, deadline):
    """Exact completed work is reusable regardless of permission for new calls."""
    ordered = sorted(enumerate(entries), key=lambda item: (
        item[1].id == prefer_other_than, item[1].tier != 'free', item[0]))
    for _, entry in ordered:
        control.check_start()
        if clock() >= deadline:
            raise RouteExhausted(deadline=True)
        hit = cache(entry)
        if hit is not None:
            control.check_inflight()
            if clock() >= deadline:
                raise RouteExhausted(deadline=True)
            return RouteResult(hit, entry, True, 0)
    return None


class MagpieRoute:
    def __init__(self, entries, *, paid_enabled, control, on_event, clock=time.monotonic):
        self.entries = tuple(entries)
        self.paid_enabled, self.control = paid_enabled, control
        self.timeout, self.on_event, self.clock = 180.0, on_event, clock
        # Reuse either group's saved results; fee permission selects new requests.
        tier = 'paid' if paid_enabled else 'free'
        self.selected = next(entry for entry in self.entries if entry.tier == tier)

    def cached_result(self, cache, *, prefer_other_than=None, deadline):
        return cached_route_result(self.entries, self.control, self.clock, cache,
                                   prefer_other_than=prefer_other_than, deadline=deadline)

    def run(self, invoke, *, cache=None, prefer_other_than=None, prepare, deadline_seconds=180.0):
        deadline = self.clock() + deadline_seconds
        self.control.check_start()
        if cache is not None:
            result = self.cached_result(cache, prefer_other_than=prefer_other_than, deadline=deadline)
            if result is not None:
                return result
        entry = self.selected
        for attempt in (1, 2):
            self.control.check_start()
            if self.clock() >= deadline:
                raise RouteExhausted(deadline=True)
            try:
                with prepare(entry):
                    self.control.check_start()
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise RouteExhausted(deadline=True)
                    self.on_event({'kind': 'route_attempt', 'entry': entry.id, 'tier': entry.tier, 'attempt': attempt})
                    value = invoke(entry, min(self.timeout, remaining), attempt)
            except RouteFailure as failure:
                self.control.check_start()
                self.on_event({'kind': 'route_failure', 'entry': entry.id, 'tier': entry.tier,
                               'category': failure.category, 'scope': failure.scope, 'attempt': attempt})
                # Only the subtitle validator can authorize a second request.
                # Transport, HTTP, refusal and uncertain responses never do.
                if attempt == 1 and failure.repairable:
                    continue
                raise RouteExhausted(deadline=failure.category in {'deadline', 'timeout'}) from None
            self.control.check_inflight()
            if self.clock() >= deadline:
                raise RouteExhausted(deadline=True)
            return RouteResult(value, entry, False, attempt)

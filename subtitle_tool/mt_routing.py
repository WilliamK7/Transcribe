"""Per-stage free/paid routing with shared admission and scoped recovery.

The caller owns the executor, cache keys, wire protocol and final commit.  In
particular, ``invoke`` must perform exactly one HTTP attempt and honor its
timeout.  No source text, response body or raw exception is emitted here.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .common import Cancelled, ToolError
from .mt_backend import MTBackend, QINIU_FREE_MODEL, qiniu_free_active


_SCOPES = frozenset({'entry', 'model', 'credential', 'endpoint', 'free_quota', 'model_quota', 'paid_account'})
_FORMAT_FAILURE_THRESHOLD = 2
_FORMAT_RETRY_SECONDS = 300.0
_CATEGORIES = frozenset({
    'timeout', 'provider_unavailable', 'provider_overloaded', 'server', 'rate_limit_exceeded',
    'context_length_exceeded', 'max_tokens_exceeded', 'token_limit_exceeded', 'string_too_long',
    'authentication', 'permission_denied', 'payment_required', 'invalid_request', 'invalid_prompt',
    'not_found', 'precondition_failed', 'payload_too_large', 'unprocessable',
    'content_policy_violation', 'refusal', 'invalid_image', 'image_too_large', 'image_too_small',
    'unsupported_image_format', 'image_not_found', 'image_download_failed', 'network', 'deadline',
    'transport', 'response_too_large', 'http_transient', 'http_permanent', 'service_unknown',
    'generation_error', 'response_structure', 'shared_wait', 'quota_observed', 'quota_refresh_failed',
    'quota_unknown', 'quota_exhausted', 'retry_after', 'format_exhausted', 'cancelled', 'local_error',
})


@dataclass(frozen=True)
class RouteEntry:
    id: str
    backend: MTBackend
    tier: str
    weight: float = 1
    rpm: int = 0

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.id):
            raise ValueError('Invalid route id')
        if not isinstance(self.backend, MTBackend) or self.tier not in {'free', 'paid'}:
            raise ValueError('Invalid route backend or tier')
        if (isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)) or
                not math.isfinite(self.weight) or self.weight <= 0):
            raise ValueError('Invalid route weight')
        if type(self.rpm) is not int or self.rpm < 0:
            raise ValueError('Invalid route RPM')


@dataclass(frozen=True)
class RouteResult:
    value: object
    entry: RouteEntry
    cached: bool
    attempts: int
    waited: float
    switches: int
    free_waited: float = 0.0


class RouteFailure(ToolError):
    """A content-free failure supplied by the single-attempt service adapter.

    ``disable`` persists only for the selected scope and this router instance.
    Leave it false for input-specific, transient and unknown errors.  A known
    Retry-After is shared at the selected scope, including across groups.
    """
    def __init__(self, category, *, scope='entry', retryable=False, retry_after=None, disable=False):
        super().__init__('翻译候选请求失败。', 'mt_route_failure')
        self.category = category if category in _CATEGORIES else 'service_unknown'
        self.scope = scope if scope in _SCOPES else 'entry'
        self.retryable = bool(retryable)
        self.disable = bool(disable)
        self.retry_after = (float(retry_after) if not isinstance(retry_after, bool) and
                            isinstance(retry_after, (int, float)) and math.isfinite(retry_after) and
                            retry_after >= 0 else None)


class RouteExhausted(ToolError):
    def __init__(self, *, deadline=False, waited=0.0):
        super().__init__('翻译阶段已超过时限，进度已保存。' if deadline else
                         '没有可用的翻译候选，进度已保存。',
                         'mt_route_deadline' if deadline else 'mt_route_exhausted')
        self.waited = waited


@dataclass
class _Admission:
    inflight: int = 0
    started: int = 0
    pending: int = 0
    starts: deque = field(default_factory=deque)


@dataclass
class _Recovery:
    generation: int = 0
    failures: int = 0
    cooldown_until: float = 0.0
    recovering: bool = False
    probe_active: bool = False


@dataclass
class _FormatHealth:
    failures: int = 0
    retry_at: float = 0.0
    generation: int = 0
    probe_active: bool = False


@dataclass
class _Ticket:
    generations: dict
    probes: tuple
    pending: bool
    token: object = field(default_factory=object)
    started_at: float | None = None
    dispatched: bool = False
    format_key: tuple | None = None
    format_generation: int = 0
    format_probe: bool = False


class _DispatchReservation:
    """Gate the actual payload handoff against concurrent route failures."""
    def __init__(self, router, entry, ticket, reservation):
        self.router, self.entry, self.ticket, self.reservation = router, entry, ticket, reservation

    def settle(self, usage):
        self.reservation.settle(usage)

    def mark_sent(self):
        if not self.router._available(self.entry):
            raise RouteFailure('provider_unavailable')
        with self.router.lock:
            now = self.router.clock()
            keys = _applicable_keys(self.entry)
            if any(key in self.router.disabled or self.router.recoveries[key].cooldown_until > now or
                   self.router.recoveries[key].generation != self.ticket.generations[key] for key in keys):
                raise RouteFailure('quota_unknown')
            # This is the short atomic dispatch boundary: after it returns the
            # transport hands its payload to the worker. Requests already
            # past this boundary remain in flight when a peer is disabled.
            self.reservation.mark_sent()
            self.ticket.dispatched = True


def _backend_identity(entry):
    backend = entry.backend
    return (backend.provider, backend.base_url, backend.model, backend.key_fingerprint, backend.request_version)


def _scope_key(entry, scope):
    backend = entry.backend
    endpoint = (backend.provider, backend.base_url)
    account = (*endpoint, backend.key_fingerprint)
    if scope == 'entry':
        return (scope, entry.id)
    if scope == 'endpoint':
        return (scope, *endpoint)
    if scope == 'model':
        return (scope, *endpoint, backend.model)
    if scope == 'model_quota':
        return (scope, *account, backend.model)
    return (scope, *account)


def _applicable_keys(entry):
    scopes = ['entry', 'model', 'credential', 'endpoint',
              'free_quota' if entry.tier == 'free' else 'paid_account']
    if entry.backend.provider == 'bailian':
        # The stop switch belongs to the account and exact model, regardless
        # of how the caller labels the route's cost tier.
        scopes.append('model_quota')
    return tuple(_scope_key(entry, scope) for scope in scopes)


class MTRouter:
    """Thread-safe pool state shared by all group stages in one run.

    ``run`` first checks each cache, then dispatches free before paid.  One
    stage has a 180 s deadline, up to 15 s of free queue/cooldown budget,
    and at most two single-HTTP invocations per backend.  The caller limits
    global concurrency, and must never hide its own retries inside ``invoke``.
    """
    def __init__(self, entries, *, paid_enabled, control, clock=time.monotonic,
                 utcnow=lambda: datetime.now(timezone.utc), wait=None,
                 priority=None, on_event=lambda event: None, free_wait_seconds=15.0):
        self.entries = tuple(entries)
        if (not all(isinstance(entry, RouteEntry) for entry in self.entries) or
                len({entry.id for entry in self.entries}) != len(self.entries) or
                len({_backend_identity(entry) for entry in self.entries}) != len(self.entries)):
            raise ValueError('Route entries and backend identities must be unique')
        if type(paid_enabled) is not bool:
            raise ValueError('paid_enabled must be a boolean')
        if (isinstance(free_wait_seconds, bool) or not isinstance(free_wait_seconds, (int, float)) or
                not math.isfinite(free_wait_seconds) or not 0 <= free_wait_seconds <= 15):
            raise ValueError('free_wait_seconds must be between 0 and 15')
        self.paid_enabled, self.control = paid_enabled, control
        self.free_wait_seconds = float(free_wait_seconds)
        self.clock, self.utcnow = clock, utcnow
        self.wait = wait if wait is not None else lambda control, seconds: control.wait(seconds)
        # Optional, nonblocking cached quota ordering: (capability group, date ordinal).
        self.priority = priority if priority is not None else lambda entry: None
        self.on_event = on_event
        self.lock = threading.Lock()
        self.states = {entry.id: _Admission() for entry in self.entries}
        self.disabled = set()
        self.recoveries = {key: _Recovery() for entry in self.entries for key in _applicable_keys(entry)}
        self._format_health = {}

    def _available(self, entry):
        return (entry.backend.provider != 'qiniu' or entry.backend.model != QINIU_FREE_MODEL or
                qiniu_free_active(self.utcnow()))

    def _select(self, tier, counts, stopped, preferred, stage):
        available = {entry.id for entry in self.entries if entry.tier == tier and self._available(entry)}
        priorities = {entry.id: self.priority(entry) for entry in self.entries if entry.id in available}
        with self.lock:
            now = self.clock()
            ready, waits = [], []
            for order, entry in enumerate(self.entries):
                identity = _backend_identity(entry)
                if (entry.id not in available or identity in stopped or counts.get(identity, 0) >= 2):
                    continue
                keys = _applicable_keys(entry)
                if any(key in self.disabled for key in keys):
                    continue
                state = self.states[entry.id]
                while state.starts and state.starts[0][0] + 61.0 <= now:
                    state.starts.popleft()
                delay = max(0.0, *(self.recoveries[key].cooldown_until - now for key in keys))
                if any(self.recoveries[key].recovering and self.recoveries[key].probe_active for key in keys):
                    delay = max(delay, 0.1)
                health = self._format_health.get((stage, entry.id)) if stage is not None else None
                if health is not None and health.probe_active:
                    delay = max(delay, 0.1)
                if entry.rpm and len(state.starts) + state.pending >= entry.rpm:
                    window_delay = (state.starts[-entry.rpm][0] + 61.0 - now
                                    if len(state.starts) >= entry.rpm else 0.1)
                    delay = max(delay, window_delay)
                if delay > 0:
                    waits.append(delay)
                else:
                    ready.append((entry, state, order))
            if ready:
                if stage is not None:
                    healthy = []
                    for item in ready:
                        health = self._format_health.get((stage, item[0].id))
                        if health is None or not health.retry_at or (health.retry_at <= now and not health.probe_active):
                            healthy.append(item)
                    # Keep free-first routing: if the entire tier is unhealthy,
                    # continue trying it instead of moving early to paid routes.
                    if healthy:
                        ready = healthy
                other = [item for item in ready if item[0].id != preferred] if preferred is not None else []
                if other:
                    ready = other
                selected = min(ready, key=lambda item: (
                    item[1].inflight / item[0].weight, item[1].started / item[0].weight, item[2]))
                selected_priority = priorities.get(selected[0].id)
                if selected_priority is not None:
                    group = selected_priority[0]
                    comparable = [item for item in ready if priorities.get(item[0].id) is not None and
                                  priorities[item[0].id][0] == group and
                                  item[1].inflight / item[0].weight == selected[1].inflight / selected[0].weight]
                    selected = min(comparable, key=lambda item: (
                        priorities[item[0].id][1] if priorities[item[0].id][1] is not None else float('inf'),
                        item[1].started / item[0].weight, item[2]))
                entry, state, _ = selected
                state.inflight += 1
                state.started += 1
                keys = _applicable_keys(entry)
                probes = tuple(key for key in keys if self.recoveries[key].recovering)
                for key in probes:
                    self.recoveries[key].probe_active = True
                ticket = _Ticket({key: self.recoveries[key].generation for key in keys}, probes, bool(entry.rpm))
                if stage is not None:
                    ticket.format_key = (stage, entry.id)
                    health = self._format_health.get(ticket.format_key)
                    if health is not None:
                        ticket.format_generation = health.generation
                        if health.retry_at and not health.probe_active:
                            health.probe_active = ticket.format_probe = True
                if entry.rpm:
                    state.pending += 1
                return entry, ticket, None
            return None, None, min(waits) if waits else None

    def _release(self, entry, ticket):
        with self.lock:
            state = self.states[entry.id]
            state.inflight -= 1
            if ticket.pending:
                state.pending -= 1
            if ticket.started_at is not None and not ticket.dispatched:
                try:
                    state.starts.remove((ticket.started_at, ticket.token))
                except ValueError:
                    pass
            for key in ticket.probes:
                recovery = self.recoveries[key]
                if ticket.generations[key] == recovery.generation:
                    recovery.probe_active = False
            if ticket.format_probe:
                health = self._format_health.get(ticket.format_key)
                if health is not None and ticket.format_generation == health.generation:
                    health.probe_active = False

    def _confirm(self, entry, ticket):
        """Reject an admission invalidated by a peer before it starts HTTP."""
        with self.lock:
            now = self.clock()
            keys = _applicable_keys(entry)
            if any(key in self.disabled or self.recoveries[key].cooldown_until > now or
                   self.recoveries[key].generation != ticket.generations[key] for key in keys):
                return False
            if ticket.pending:
                state = self.states[entry.id]
                state.pending -= 1
                state.starts.append((now, ticket.token))
                ticket.pending = False
                ticket.started_at = now
            return True

    def _record_failure(self, entry, ticket, failure):
        with self.lock:
            if failure.category == 'format_exhausted' and ticket.dispatched and ticket.format_key is not None:
                health = self._format_health.setdefault(ticket.format_key, _FormatHealth())
                if ticket.format_generation == health.generation:
                    health.failures += 1
                    if health.failures >= _FORMAT_FAILURE_THRESHOLD:
                        health.retry_at = self.clock() + _FORMAT_RETRY_SECONDS
                        health.generation += 1
                        health.probe_active = False
            key = _scope_key(entry, failure.scope)
            if failure.disable:
                self.disabled.add(key)
            if not failure.retryable and failure.retry_after is None:
                return
            now = self.clock()
            recovery = self.recoveries.setdefault(key, _Recovery())
            if ticket.generations.get(key, 0) == recovery.generation:
                recovery.generation += 1
                recovery.failures += 1
                delay = (failure.retry_after if failure.retry_after is not None else
                         min(60.0, 15.0 * 2 ** min(recovery.failures - 1, 2)))
                recovery.cooldown_until = max(recovery.cooldown_until, now + delay)
                recovery.recovering = True
                recovery.probe_active = False
            elif failure.retry_after is not None and now + failure.retry_after > recovery.cooldown_until:
                # A late response with a newer Retry-After invalidates any current probe.
                recovery.generation += 1
                recovery.cooldown_until = now + failure.retry_after
                recovery.recovering = True
                recovery.probe_active = False

    def _record_success(self, ticket):
        with self.lock:
            health = self._format_health.get(ticket.format_key)
            if health is not None and ticket.format_generation == health.generation:
                health.failures = 0
                health.retry_at = 0.0
                health.probe_active = False
                health.generation += 1
            for key in ticket.probes:
                recovery = self.recoveries[key]
                if ticket.generations[key] == recovery.generation:
                    recovery.cooldown_until = 0.0
                    recovery.failures = 0
                    recovery.recovering = False
                    recovery.probe_active = False

    def cached_result(self, cache, *, prefer_other_than=None, deadline):
        """Check exact caches independently of permission to send new requests."""
        ordered = sorted(enumerate(self.entries), key=lambda item: (
            item[1].id == prefer_other_than, item[1].tier != 'free', item[0]))
        for _, entry in ordered:
            self.control.check_start()
            if self.clock() >= deadline:
                raise RouteExhausted(deadline=True)
            hit = cache(entry)
            if hit is not None:
                self.control.check_inflight()
                if self.clock() >= deadline:
                    raise RouteExhausted(deadline=True)
                return RouteResult(hit, entry, True, 0, 0.0, 0)
        return None

    def run(self, invoke, *, cache=None, prefer_other_than=None, prepare=None, deadline_seconds=180.0, stage=None):
        """Return a RouteResult; raise a content-free error when all routes fail."""
        if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)) or
                not math.isfinite(deadline_seconds) or not 0 <= deadline_seconds <= 180):
            raise ValueError('deadline_seconds must be between 0 and 180')
        if stage is not None and (not isinstance(stage, str) or not stage):
            raise ValueError('Invalid route stage')
        deadline = self.clock() + deadline_seconds
        self.control.check_start()
        if cache is not None:
            result = self.cached_result(cache, prefer_other_than=prefer_other_than, deadline=deadline)
            if result is not None:
                return result

        counts, stopped = {}, set()
        attempts = switches = 0
        waited = free_waited = 0.0
        previous = None
        tier = 'free'
        while True:
            self.control.check_start()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise RouteExhausted(deadline=True, waited=waited)
            if tier == 'paid' and not self.paid_enabled:
                raise RouteExhausted(waited=waited)
            entry, ticket, delay = self._select(tier, counts, stopped, prefer_other_than, stage)
            if entry is None:
                budget = self.free_wait_seconds - free_waited if tier == 'free' else remaining
                if delay is None or budget <= 0:
                    if tier == 'free':
                        tier = 'paid'
                        continue
                    raise RouteExhausted(waited=waited)
                interval = min(delay, budget, remaining)
                self.on_event({'kind': 'route_wait', 'tier': tier, 'seconds': interval})
                before = self.clock()
                self.wait(self.control, interval)
                actual = max(0.0, self.clock() - before)
                waited += actual
                if tier == 'free':
                    free_waited += actual
                continue

            try:
                self.control.check_start()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise RouteExhausted(deadline=True, waited=waited)
                identity = _backend_identity(entry)
                attempt = counts.get(identity, 0) + 1
                if previous is not None and previous != entry.id:
                    self.on_event({'kind': 'route_switch', 'from': previous, 'to': entry.id})
                self.on_event({'kind': 'route_attempt', 'entry': entry.id, 'tier': tier, 'attempt': attempt})
                self.control.check_start()
                failure = None
                with ExitStack() as context:
                    reservation = None
                    if prepare is not None:
                        before = self.clock()
                        reported_wait = None
                        try:
                            allowance = max(0.0, min(deadline - before,
                                self.free_wait_seconds - free_waited if tier == 'free' else deadline - before))
                            reservation = context.enter_context(prepare(entry, allowance))
                        except Cancelled:
                            raise
                        except RouteFailure as error:
                            reported_wait = getattr(error, 'preparation_waited', None)
                            failure = error
                        finally:
                            actual = max(0.0, self.clock() - before)
                            if reservation is not None:
                                reported_wait = getattr(reservation, 'preparation_waited', reported_wait)
                            if (isinstance(reported_wait, (int, float)) and not isinstance(reported_wait, bool)
                                    and math.isfinite(reported_wait) and reported_wait >= 0):
                                actual = min(actual, reported_wait)
                            waited += actual
                            if tier == 'free':
                                free_waited += actual
                    self.control.check_start()
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise RouteExhausted(deadline=True, waited=waited)
                    if tier == 'free' and free_waited > self.free_wait_seconds:
                        tier = 'paid'
                        continue
                    if failure is None and reservation is not None:
                        try:
                            reservation.claim()
                        except Cancelled:
                            raise
                        except RouteFailure as error:
                            failure = error
                    if failure is None:
                        self.control.check_start()
                        if not self._available(entry) or not self._confirm(entry, ticket):
                            continue
                        remaining = deadline - self.clock()
                        if remaining <= 0:
                            raise RouteExhausted(deadline=True, waited=waited)
                        counts[identity] = attempt
                        attempts += 1
                        if previous is not None and previous != entry.id:
                            switches += 1
                        previous = entry.id
                        gated = reservation is not None
                        dispatch = _DispatchReservation(self, entry, ticket, reservation) if gated else reservation
                        if not gated:
                            ticket.dispatched = True
                        try:
                            value = (invoke(entry, min(60.0, remaining), attempt, dispatch) if prepare is not None
                                     else invoke(entry, min(60.0, remaining), attempt))
                        except Cancelled:
                            raise
                        except RouteFailure as error:
                            if not ticket.dispatched:
                                counts[identity] -= 1
                                attempts -= 1
                            failure = error
                        else:
                            self.control.check_inflight()
                            if self.clock() >= deadline:
                                raise RouteExhausted(deadline=True, waited=waited)
                            self._record_success(ticket)
                            return RouteResult(value, entry, False, attempts, waited, switches, free_waited)
                    self._record_failure(entry, ticket, failure)
            finally:
                self._release(entry, ticket)

            self.control.check_start()
            self.on_event({'kind': 'route_failure', 'entry': entry.id, 'tier': tier,
                           'category': failure.category, 'scope': failure.scope, 'attempt': attempt})
            if failure.disable or not failure.retryable:
                stopped.add(identity)

"""Bounded OpenRouter free-quota admission for one translation run.

The account ledger, observation and reservation live together. The router
owns retries and the time budget, and claims a reservation before dispatch.
"""
from __future__ import annotations

import json
import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .common import Cancelled, ToolError
from .mt_backend import normalize_mt_url
from .mt_routing import RouteFailure
from .mt_transport import HTTPReply, ProcessTransport, TransportError


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
        self.observed_monotonic = None

    def trusted(self, control):
        return (self.day == self.utcnow().date() and self.observed_monotonic is not None and
                control.created <= self.observed_monotonic)

    @staticmethod
    def _read(reply: HTTPReply):
        if reply.status != 200:
            raise ValueError()
        body = json.loads(reply.body)
        value = body['data']['free_model_daily_requests']
        if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] < 0
                                              for k in ('used', 'limit', 'remaining')):
            raise ValueError()
        return {k: value[k] for k in ('used', 'limit', 'remaining')}

    def _finish_refresh(self, value, day, sent, active):
        """Commit one observation and account for reservations made during its query."""
        with self.condition:
            observed = self.utcnow()
            if value is not None and observed.date() == day:
                remote = min(value['remaining'], max(0, value['limit'] - value['used']))
                available = max(0, remote - active - (self.reservations - sent))
                self.remaining = min(self.remaining, available) if self.day == day else available
                self.day = day
                self.observed_monotonic = self.clock()
                category = 'quota_observed'
            else:
                category = 'quota_refresh_failed'
            self.refreshing = False
            self.condition.notify_all()
        return category


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


class _NoopReservation:
    preparation_waited = 0.0

    def claim(self):
        pass

    def mark_sent(self):
        pass


class _Reservation:
    def __init__(self, ledger, control, preparation_waited):
        self.ledger, self.control = ledger, control
        self.preparation_waited = preparation_waited
        self.claimed = self.sent = False
        self.day = self.observed = None

    def claim(self):
        if self.claimed:
            raise ValueError('Quota reservation already claimed')
        ledger = self.ledger
        with ledger.condition:
            self.control.check_start()
            if (not ledger.trusted(self.control) or
                    not ledger.refreshing and (ledger.last_attempt is None or ledger.clock() - ledger.last_attempt >= 30)):
                raise RouteFailure('quota_unknown', scope='free_quota')
            if ledger.remaining <= 0:
                raise RouteFailure('quota_exhausted', scope='free_quota')
            ledger.remaining -= 1
            ledger.inflight += 1
            ledger.reservations += 1
            self.claimed = True
            self.day, self.observed = ledger.day, ledger.observed_monotonic

    def mark_sent(self):
        if not self.claimed:
            raise ValueError('Quota reservation not claimed')
        self.sent = True

    def close(self):
        if not self.claimed:
            return
        with self.ledger.condition:
            if (not self.sent and self.ledger.day == self.day and
                    self.ledger.observed_monotonic == self.observed):
                # A concurrent refresh may have clamped a negative balance;
                # never invent availability in a different observation.
                self.ledger.remaining += 1
            self.ledger.inflight -= 1
            self.claimed = False
            self.ledger.condition.notify_all()


class OpenRouterQuota:
    """Query once per shared account observation, reserving only before POST.

    ``credential(entry)`` returns the frozen backend's current key.  This
    callback and the transport are never called for a cache hit, paid route,
    Qiniu route, or a non-free OpenRouter model.
    """
    def __init__(self, control, credential, *, transport=None, clock=time.monotonic,
                 ledger_for=None, on_event=lambda event: None):
        self.control, self.credential, self.clock = control, credential, clock
        self.transport = transport if transport is not None else ProcessTransport()
        self.ledger_for = ledger_for if ledger_for is not None else quota_for
        self.on_event = on_event

    @contextmanager
    def reserve(self, entry, wait_budget):
        if (isinstance(wait_budget, bool) or not isinstance(wait_budget, (int, float)) or
                not math.isfinite(wait_budget) or wait_budget < 0):
            raise ValueError('Invalid quota wait budget')
        if entry.tier != 'free' or not entry.backend.free_quota:
            yield _NoopReservation()
            return
        ledger = self.ledger_for(entry.backend)
        deadline = self.clock() + wait_budget
        measured = [0.0]
        try:
            self._ensure(ledger, entry, deadline, measured)
        except ToolError as error:
            error.preparation_waited = measured[0]
            raise
        reservation = _Reservation(ledger, self.control, measured[0])
        try:
            yield reservation
        finally:
            reservation.close()

    def _ensure(self, ledger, entry, deadline, measured):
        def pause(delay):
            started = self.clock()
            try:
                ledger.condition.wait(delay)
            finally:
                measured[0] += max(0.0, self.clock() - started)

        while True:
            self.control.check_start()
            with ledger.condition:
                day, now = ledger.utcnow().date(), ledger.clock()
                trusted = ledger.trusted(self.control)
                if trusted and ledger.remaining <= 0:
                    raise RouteFailure('quota_exhausted', scope='free_quota')
                if ledger.refreshing:
                    if trusted:
                        return
                    delay = min(.1, max(0.0, deadline - self.clock()))
                    if delay <= 0:
                        raise RouteFailure('quota_unknown', scope='free_quota')
                    pause(delay)
                    continue
                due = ledger.last_attempt is None or now - ledger.last_attempt >= 30
                if not due:
                    if trusted:
                        return
                    if ledger.observed_monotonic is None:
                        raise RouteFailure('quota_unknown', scope='free_quota')
                    delay = min(.1, max(0.0, deadline - self.clock()), ledger.last_attempt + 30 - now)
                    if delay <= 0:
                        raise RouteFailure('quota_unknown', scope='free_quota')
                    pause(delay)
                    continue
                if deadline - self.clock() <= 0:
                    raise RouteFailure('quota_unknown', scope='free_quota')
                ledger.refreshing = True
                ledger.last_attempt = now
                sent, active = ledger.reservations, ledger.inflight
                break

        value, authentication = None, False
        try:
            try:
                key = self.credential(entry)
            except ToolError:
                authentication = True
            if not authentication:
                remaining = deadline - self.clock()
                if remaining > 0:
                    self.control.check_start()
                    self.on_event({'kind': 'quota_query'})
                    started = self.clock()
                    try:
                        reply = self.transport.request('GET', entry.backend.base_url + '/key',
                            headers={'Authorization': 'Bearer ' + key}, control=self.control,
                            timeout=min(15.0, remaining), drain=False)
                    finally:
                        measured[0] += max(0.0, self.clock() - started)
                    if reply.status == 401:
                        authentication = True
                    else:
                        value = ledger._read(reply)
        except Cancelled:
            raise
        except (TransportError, ValueError, KeyError, TypeError, RecursionError):
            pass
        finally:
            category = ledger._finish_refresh(value, day, sent, active)
            self.on_event({'kind': 'runtime_quota', 'category': category})

        self.control.check_start()
        if authentication:
            raise RouteFailure('authentication', scope='credential', disable=True)
        if self.clock() > deadline:
            raise RouteFailure('quota_unknown', scope='free_quota')
        with ledger.condition:
            if not ledger.trusted(self.control):
                raise RouteFailure('quota_unknown', scope='free_quota')
            if ledger.remaining <= 0:
                raise RouteFailure('quota_exhausted', scope='free_quota')

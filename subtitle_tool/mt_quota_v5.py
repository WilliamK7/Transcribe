"""Bounded OpenRouter free-quota admission for one v5 translation run.

This shares the legacy account ledger and parser but never wraps an HTTP
translation or retries it.  The router owns the time budget and invokes the
returned reservation immediately before dispatch.
"""
from __future__ import annotations

import math
import time
from contextlib import contextmanager

from .common import Cancelled, ToolError
from .mt_routing import RouteFailure
from .mt_runtime import ServiceError, runtime_resources
from .mt_transport import ProcessTransport, TransportError


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
        with self.ledger.condition:
            try:
                ready = self.ledger.try_reserve(self.control)
            except ServiceError as error:
                raise RouteFailure(error.category, scope='free_quota') from None
            if not ready:
                raise RouteFailure('quota_unknown', scope='free_quota')
            self.claimed = True
            self.day, self.observed = self.ledger.day, self.ledger.observed_monotonic

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


class V5Quota:
    """Query once per shared account observation, reserving only before POST.

    ``credential(entry)`` returns the frozen backend's current key.  This
    callback and the transport are never called for a cache hit, paid route,
    Qiniu route, or a non-free OpenRouter model.
    """
    def __init__(self, control, credential, *, transport=None, clock=time.monotonic,
                 ledger_for=None, on_event=lambda event: None):
        self.control, self.credential, self.clock = control, credential, clock
        self.transport = transport if transport is not None else ProcessTransport()
        self.ledger_for = ledger_for if ledger_for is not None else lambda backend: runtime_resources(backend)[1]
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
                if (ledger.day == day and ledger.observed_monotonic is not None and
                        self.control.created <= ledger.observed_monotonic):
                    ledger.trusted.add(self.control)
                trusted = ledger.day == day and self.control in ledger.trusted
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
                    if ledger.observation is None:
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
            event = ledger._finish_refresh(self.control, value, day, sent, active)
            self.on_event({'kind': 'runtime_quota', 'category': event['category']})

        self.control.check_start()
        if authentication:
            raise RouteFailure('authentication', scope='credential', disable=True)
        if self.clock() > deadline:
            raise RouteFailure('quota_unknown', scope='free_quota')
        with ledger.condition:
            if ledger.day != ledger.utcnow().date() or self.control not in ledger.trusted:
                raise RouteFailure('quota_unknown', scope='free_quota')
            if ledger.remaining <= 0:
                raise RouteFailure('quota_exhausted', scope='free_quota')

"""A translation run's stop signal and shared, bounded drain window."""
from __future__ import annotations

import threading
import time

from .common import Cancelled


class MTControl:
    def __init__(self, cancel, *, clock=time.monotonic, drain_seconds=5.0):
        self.cancel = cancel
        self.clock = clock
        self.created = clock()
        self.drain_seconds = drain_seconds
        self.lock = threading.RLock()
        self.deadline = None
        self.error = None

    def begin(self):
        """Only called between jobs, after the previous job's workers have joined."""
        with self.lock:
            self.deadline = None
            self.error = None
            self._observe()

    def _observe(self):
        if self.cancel.is_set() and self.deadline is None:
            self.deadline = self.clock() + self.drain_seconds

    @property
    def stopping(self):
        with self.lock:
            self._observe()
            return self.deadline is not None

    def cancel_user(self, *, force=False):
        with self.lock:
            self.cancel.set()
            self._observe()
            if force:
                self.deadline = self.clock()

    def stop(self, error, *, force=False):
        with self.lock:
            self._observe()
            if self.error is None:
                self.error = error
            if self.deadline is None:
                self.deadline = self.clock() + self.drain_seconds
            if force:
                self.deadline = self.clock()

    def _raise(self):
        if self.cancel.is_set():
            raise Cancelled()
        raise self.error

    def check_start(self):
        with self.lock:
            self._observe()
            if self.deadline is not None:
                self._raise()

    def check_inflight(self, *, drain=True):
        with self.lock:
            self._observe()
            if self.deadline is not None and (not drain or self.clock() >= self.deadline):
                self._raise()

    def wait(self, seconds):
        until = self.clock() + seconds
        while True:
            self.check_start()
            remaining = until - self.clock()
            if remaining <= 0:
                return
            self.cancel.wait(min(remaining, .1))

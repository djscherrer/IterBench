"""Base types for diagnostics collection during a bench run."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod


class DiagnosticsCollector(ABC):
    """Background diagnostics sampling; call ``start()`` then ``stop()``."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def start(self) -> None:
        if self._started:
            return
        self._stop_event.clear()
        self._threads = self._build_threads()
        for t in self._threads:
            t.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=120)
        self._threads = []
        self._started = False

    def __enter__(self) -> DiagnosticsCollector:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @abstractmethod
    def _build_threads(self) -> list[threading.Thread]:
        raise NotImplementedError


class DiagnosticsSession:
    """Start and stop several collectors together (LIFO shutdown)."""

    def __init__(self, collectors: list[DiagnosticsCollector]) -> None:
        self._collectors = collectors

    def start(self) -> None:
        for c in self._collectors:
            c.start()

    def stop(self) -> None:
        for c in reversed(self._collectors):
            c.stop()

    def __enter__(self) -> DiagnosticsSession:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

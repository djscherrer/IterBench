"""Base types for utilization capture during a perf run."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path


class UtilizationLogger(ABC):
    """Background utilization sampling; call ``start()`` then ``stop()``."""

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

    def __enter__(self) -> UtilizationLogger:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @abstractmethod
    def _build_threads(self) -> list[threading.Thread]:
        raise NotImplementedError


class UtilizationSession:
    """Start/stop several loggers together."""

    def __init__(self, loggers: list[UtilizationLogger]) -> None:
        self._loggers = loggers

    def start(self) -> None:
        for lg in self._loggers:
            lg.start()

    def stop(self) -> None:
        for lg in reversed(self._loggers):
            lg.stop()

    def __enter__(self) -> UtilizationSession:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def stats_root(run_dir: Path) -> Path:
    root = run_dir / "stats"
    root.mkdir(parents=True, exist_ok=True)
    return root

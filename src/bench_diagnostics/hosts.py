"""SSH host metrics for Locust load-generator machines (both bench modes)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Sequence

import remote_exec
from bench_models import host_slug

from .base import DiagnosticsCollector
from .paths import load_host_dir


class LoadHostMetricsCollector(DiagnosticsCollector):
    """Sample ``host_performance.csv`` on each Locust SSH host."""

    def __init__(
        self,
        run_dir: Path,
        load_hosts: Sequence[str],
        *,
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._load_hosts = tuple(h for h in load_hosts if h)
        self._interval_s = interval_s

    def _build_threads(self) -> list[threading.Thread]:
        threads: list[threading.Thread] = []
        for host in self._load_hosts:
            slug = host_slug(host)
            out_dir = load_host_dir(self._run_dir, slug)
            threads.append(
                threading.Thread(
                    target=remote_exec.capture_host_performance,
                    args=(self._run_dir, host, self._log, self._stop_event),
                    kwargs={
                        "out_csv": out_dir / "host_performance.csv",
                        "interval": self._interval_s,
                    },
                    daemon=True,
                    name=f"diag-load-host-{slug}",
                )
            )
        return threads

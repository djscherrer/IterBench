"""SSH host metrics on Locust load-generator machines (all bench modes)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Sequence

import remote_exec
from bench_models import host_slug

from .base import UtilizationLogger, stats_root


class LoadHostUtilizationLogger(UtilizationLogger):
    """
    Machine-level stats on load master/worker SSH hosts.

    Same capture as the load-host portion of the old ``LocustRunner`` metrics loop.
    """

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
            host_dir = stats_root(self._run_dir) / host_slug(host)
            host_dir.mkdir(parents=True, exist_ok=True)
            out_csv = host_dir / "host_performance.csv"
            threads.append(
                threading.Thread(
                    target=remote_exec.capture_host_performance,
                    args=(self._run_dir, host, self._log, self._stop_event),
                    kwargs={"out_csv": out_csv, "interval": self._interval_s},
                    daemon=True,
                    name=f"load-host-perf-{host_slug(host)}",
                )
            )
        return threads

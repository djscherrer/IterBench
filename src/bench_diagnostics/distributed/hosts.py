"""SSH host metrics for distributed_bench workload machines (backend / DB / LB)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import remote_exec
from bench_models import host_slug

from ..base import DiagnosticsCollector
from ..paths import distributed_host_dir


@dataclass(frozen=True)
class WorkloadHostSpec:
    """One workload SSH host with optional docker stats and socket queues."""

    host: str
    docker_container: str | None = None
    socket_ports: tuple[int, ...] = field(default_factory=tuple)


class WorkloadHostMetricsCollector(DiagnosticsCollector):
    """Sample host metrics under ``diagnostics/distributed/hosts/<slug>/``."""

    def __init__(
        self,
        run_dir: Path,
        host_specs: Sequence[WorkloadHostSpec],
        *,
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._specs = tuple(s for s in host_specs if s.host)
        self._interval_s = interval_s

    def _build_threads(self) -> list[threading.Thread]:
        threads: list[threading.Thread] = []
        for spec in self._specs:
            slug = host_slug(spec.host)
            out_dir = distributed_host_dir(self._run_dir, slug)
            threads.append(
                threading.Thread(
                    target=remote_exec.capture_host_performance,
                    args=(self._run_dir, spec.host, self._log, self._stop_event),
                    kwargs={
                        "out_csv": out_dir / "host_performance.csv",
                        "interval": self._interval_s,
                        "docker_container": spec.docker_container,
                    },
                    daemon=True,
                    name=f"diag-workload-host-{slug}",
                )
            )
            if spec.socket_ports:
                threads.append(
                    threading.Thread(
                        target=remote_exec.capture_socket_queues,
                        args=(self._run_dir, spec.host, self._log, self._stop_event),
                        kwargs={
                            "ports": list(spec.socket_ports),
                            "out_csv": out_dir / "socket_queue.csv",
                            "interval": self._interval_s,
                        },
                        daemon=True,
                        name=f"diag-sock-{slug}",
                    )
                )
        return threads

"""SSH host + Docker stats for distributed_bench app/DB/LB hosts."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Mapping, Sequence

import remote_exec
from bench_models import host_slug

from .base import UtilizationLogger, stats_root


class DistributedBenchUtilizationLogger(UtilizationLogger):
    """
    Machine-level stats on backend/DB/LB hosts (not load generators).

    Includes optional ``docker stats`` CPU%% and socket queue sampling on app ports.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        backend_hosts: Sequence[str],
        app_port: int,
        needs_db: bool,
        db_host: str | None = None,
        lb_host: str | None = None,
        backend_container_names: Mapping[str, str] | None = None,
        db_container_name: str | None = None,
        lb_container_name: str | None = None,
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._backend_hosts = tuple(backend_hosts)
        self._app_port = int(app_port)
        self._needs_db = needs_db
        self._db_host = (db_host or "").strip() or None
        self._lb_host = (lb_host or "").strip() or None
        self._backend_containers = dict(backend_container_names or {})
        self._db_container = db_container_name
        self._lb_container = lb_container_name
        self._interval_s = interval_s

    def _perf_hosts(self) -> list[str]:
        hosts: list[str] = list(self._backend_hosts)
        if self._needs_db and self._db_host:
            hosts.append(self._db_host)
        if self._lb_host:
            hosts.append(self._lb_host)
        seen: set[str] = set()
        return [h for h in hosts if not (h in seen or seen.add(h))]

    def _docker_container_for_host(self, host: str) -> str | None:
        if host in self._backend_containers:
            return self._backend_containers[host]
        if self._needs_db and host == self._db_host:
            return self._db_container
        if host == self._lb_host:
            return self._lb_container
        return None

    def _build_threads(self) -> list[threading.Thread]:
        threads: list[threading.Thread] = []
        for host in self._perf_hosts():
            host_dir = stats_root(self._run_dir) / host_slug(host)
            host_dir.mkdir(parents=True, exist_ok=True)
            threads.append(
                threading.Thread(
                    target=remote_exec.capture_host_performance,
                    args=(self._run_dir, host, self._log, self._stop_event),
                    kwargs={
                        "out_csv": host_dir / "host_performance.csv",
                        "interval": self._interval_s,
                        "docker_container": self._docker_container_for_host(host),
                    },
                    daemon=True,
                    name=f"dist-host-perf-{host_slug(host)}",
                )
            )
            ports: list[int] = []
            if host in self._backend_hosts or host == self._lb_host:
                ports.append(self._app_port)
            if self._needs_db and host == self._db_host:
                ports.append(5432)
            if ports:
                threads.append(
                    threading.Thread(
                        target=remote_exec.capture_socket_queues,
                        args=(self._run_dir, host, self._log, self._stop_event),
                        kwargs={
                            "ports": ports,
                            "out_csv": host_dir / "socket_queue.csv",
                            "interval": self._interval_s,
                        },
                        daemon=True,
                        name=f"dist-sock-{host_slug(host)}",
                    )
                )
        return threads

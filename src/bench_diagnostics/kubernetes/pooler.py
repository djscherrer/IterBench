"""PgBouncer ``SHOW POOLS`` sampling for write and read poolers."""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from bench_diagnostics import _kubectl
from bench_diagnostics.base import DiagnosticsCollector
from bench_diagnostics.paths import kubernetes_pooler_dir

_POOLS_HEADER = (
    "ts_epoch_s,ts,pooler_role,pod,database,user,cl_active,cl_waiting,"
    "sv_active,sv_idle,sv_used,sv_tested,sv_login,maxwait_s\n"
)


def _resolve_pooler_pods(
    *, namespace: str, role: str, logger: logging.Logger
) -> list[str]:
    proc = _kubectl.run(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"baxbench.dev/role={role}",
            "-o",
            "jsonpath={.items[?(@.status.phase==\"Running\")].metadata.name}",
        ],
        timeout_s=30,
    )
    if proc.returncode != 0:
        logger.warning(
            "could not resolve pooler pods (ns=%s role=%s): %s",
            namespace,
            role,
            (proc.stderr or proc.stdout or "").strip()[:200],
        )
        return []
    return [n for n in (proc.stdout or "").split() if n]


def _show_pools(
    *,
    namespace: str,
    pod: str,
    port: int,
    user: str,
    password: str,
    timeout_s: int = 15,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            pod,
            "--",
            "env",
            f"PGPASSWORD={password}",
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            "pgbouncer",
            "-X",
            "-q",
            "-t",
            "-A",
            "-F",
            ",",
            "-c",
            "SHOW POOLS;",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _capture_pooler_pools(
    *,
    namespace: str,
    role: str,
    port: int,
    user: str,
    password: str,
    out_csv: Path,
    stop_event: threading.Event,
    interval_s: int,
    logger: logging.Logger,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not out_csv.is_file() or out_csv.stat().st_size == 0:
        out_csv.write_text(_POOLS_HEADER, encoding="utf-8")
    warned_exec = False
    while not stop_event.is_set():
        loop_start = time.time()
        pods = _resolve_pooler_pods(namespace=namespace, role=role, logger=logger)
        if not pods:
            stop_event.wait(timeout=interval_s)
            continue
        ts_epoch = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(out_csv, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for pod in pods:
                rc, out, err = _show_pools(
                    namespace=namespace,
                    pod=pod,
                    port=port,
                    user=user,
                    password=password,
                )
                if rc != 0:
                    if not warned_exec:
                        logger.warning(
                            "SHOW POOLS failed (role=%s pod=%s): %s",
                            role,
                            pod,
                            (err or out).strip()[:200],
                        )
                        warned_exec = True
                    continue
                for line in out.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 11:
                        continue
                    writer.writerow(
                        [
                            f"{ts_epoch:.3f}",
                            ts,
                            role,
                            pod,
                            parts[0],
                            parts[1],
                            parts[2],
                            parts[3],
                            parts[4],
                            parts[5],
                            parts[6],
                            parts[7],
                            parts[8],
                            parts[9],
                            parts[10] if len(parts) > 10 else "0",
                        ]
                    )
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


class PgBouncerMetricsCollector(DiagnosticsCollector):
    """Sample ``SHOW POOLS`` from write and read PgBouncer pods when present."""

    def __init__(
        self,
        run_dir: Path,
        *,
        namespace: str,
        user: str = "postgres",
        password: str = "postgres",
        pooler_port: int = 6432,
        read_pooler_port: int = 6432,
        pooler_enabled: bool = True,
        read_pooler_enabled: bool = True,
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._namespace = namespace.strip()
        self._user = user
        self._password = password
        self._pooler_port = pooler_port
        self._read_pooler_port = read_pooler_port
        self._pooler_enabled = pooler_enabled
        self._read_pooler_enabled = read_pooler_enabled
        self._interval_s = interval_s

    def _build_threads(self) -> list[threading.Thread]:
        threads: list[threading.Thread] = []
        if not self._pooler_enabled and not self._read_pooler_enabled:
            return threads
        out_csv = kubernetes_pooler_dir(self._run_dir) / "pgbouncer_pools.csv"
        shared = {
            "namespace": self._namespace,
            "user": self._user,
            "password": self._password,
            "out_csv": out_csv,
            "stop_event": self._stop_event,
            "interval_s": self._interval_s,
            "logger": self._log,
        }
        if self._pooler_enabled:
            threads.append(
                threading.Thread(
                    target=_capture_pooler_pools,
                    kwargs={**shared, "role": "pooler", "port": self._pooler_port},
                    daemon=True,
                    name="diag-pgbouncer-write",
                )
            )
        if self._read_pooler_enabled:
            threads.append(
                threading.Thread(
                    target=_capture_pooler_pools,
                    kwargs={
                        **shared,
                        "role": "read-pooler",
                        "port": self._read_pooler_port,
                    },
                    daemon=True,
                    name="diag-pgbouncer-read",
                )
            )
        return threads

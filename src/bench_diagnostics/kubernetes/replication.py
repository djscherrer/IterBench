"""``pg_stat_replication`` sampling on the Postgres primary."""

from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path

from bench_diagnostics.kubernetes.database import (
    _psql_exec,
    _resolve_postgres_pod,
)
from bench_diagnostics.base import DiagnosticsCollector
from bench_diagnostics.paths import kubernetes_database_dir

_REPLICATION_HEADER = (
    "ts_epoch_s,ts,application_name,state,sync_state,replay_lag_s,flush_lag_s\n"
)

_REPLICATION_SQL = (
    "SELECT application_name, state, sync_state, "
    "COALESCE(EXTRACT(epoch FROM replay_lag), 0), "
    "COALESCE(EXTRACT(epoch FROM flush_lag), 0) "
    "FROM pg_stat_replication;"
)


def _capture_pg_stat_replication(
    *,
    namespace: str,
    label_selector: str,
    user: str,
    password: str,
    db: str,
    out_csv: Path,
    stop_event: threading.Event,
    interval_s: int,
    logger: logging.Logger,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not out_csv.is_file() or out_csv.stat().st_size == 0:
        out_csv.write_text(_REPLICATION_HEADER, encoding="utf-8")
    pod: str | None = None
    warned_exec = False
    while not stop_event.is_set():
        loop_start = time.time()
        if not pod:
            pod = _resolve_postgres_pod(
                namespace=namespace, label_selector=label_selector, logger=logger
            )
            if not pod:
                stop_event.wait(timeout=interval_s)
                continue
        rc, out, err = _psql_exec(
            namespace=namespace,
            pod=pod,
            user=user,
            db=db,
            password=password,
            sql=_REPLICATION_SQL,
        )
        if rc != 0:
            if not warned_exec:
                logger.warning(
                    "psql pg_stat_replication failed (pod=%s): %s",
                    pod,
                    (err or out).strip()[:200],
                )
                warned_exec = True
            pod = None
        else:
            ts_epoch = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(out_csv, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                for line in out.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 5:
                        continue
                    writer.writerow(
                        [f"{ts_epoch:.3f}", ts, parts[0], parts[1], parts[2], parts[3], parts[4]]
                    )
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


class ReplicationMetricsCollector(DiagnosticsCollector):
    """Sample replication lag from the primary when read replicas exist."""

    def __init__(
        self,
        run_dir: Path,
        *,
        namespace: str,
        label_selector: str,
        user: str = "postgres",
        password: str = "postgres",
        database: str = "testdb",
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._namespace = namespace.strip()
        self._label_selector = label_selector
        self._user = user
        self._password = password
        self._database = database
        self._interval_s = interval_s

    def _build_threads(self) -> list[threading.Thread]:
        out_dir = kubernetes_database_dir(self._run_dir)
        shared = {
            "namespace": self._namespace,
            "label_selector": self._label_selector,
            "user": self._user,
            "password": self._password,
            "db": self._database,
            "stop_event": self._stop_event,
            "interval_s": self._interval_s,
            "logger": self._log,
        }
        return [
            threading.Thread(
                target=_capture_pg_stat_replication,
                kwargs={**shared, "out_csv": out_dir / "pg_stat_replication.csv"},
                daemon=True,
                name="diag-pg-replication",
            ),
        ]

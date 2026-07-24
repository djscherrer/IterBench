"""Postgres ``pg_stat_*`` sampling under ``diagnostics/kubernetes/database/``."""

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
from bench_diagnostics.paths import kubernetes_database_dir


_PG_STAT_ACTIVITY_HEADER = "ts_epoch_s,ts,state,count,max_age_s\n"
_PG_STAT_DATABASE_HEADER = (
    "ts_epoch_s,ts,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,"
    "tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,"
    "conflicts,deadlocks,temp_files,temp_bytes\n"
)


def _resolve_postgres_pod(
    *, namespace: str, label_selector: str, logger: logging.Logger
) -> str | None:
    proc = _kubectl.run(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            label_selector,
            "-o",
            "jsonpath={.items[?(@.status.phase==\"Running\")].metadata.name}",
        ],
        timeout_s=30,
    )
    if proc.returncode != 0:
        logger.warning(
            "could not resolve postgres pod (ns=%s sel=%s): %s",
            namespace,
            label_selector,
            (proc.stderr or proc.stdout or "").strip()[:200],
        )
        return None
    names = (proc.stdout or "").split()
    return names[0] if names else None


def _psql_exec(
    *,
    namespace: str,
    pod: str,
    user: str,
    db: str,
    password: str,
    sql: str,
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
            "-U",
            user,
            "-d",
            db,
            "-X",
            "-q",
            "-t",
            "-A",
            "-F",
            ",",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _capture_pg_stat_activity(
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
        out_csv.write_text(_PG_STAT_ACTIVITY_HEADER, encoding="utf-8")
    pod: str | None = None
    warned_resolve = False
    warned_exec = False
    sql = (
        "SELECT COALESCE(state, 'unknown'), count(*), "
        "COALESCE(MAX(EXTRACT(epoch FROM (now() - query_start))), 0) "
        "FROM pg_stat_activity "
        "WHERE datname = $$" + db.replace("$$", "") + "$$ "
        "GROUP BY state;"
    )
    while not stop_event.is_set():
        loop_start = time.time()
        if not pod:
            pod = _resolve_postgres_pod(
                namespace=namespace, label_selector=label_selector, logger=logger
            )
            if not pod:
                if not warned_resolve:
                    logger.warning(
                        "pg_stat_activity sampling: no running postgres pod yet"
                    )
                    warned_resolve = True
                stop_event.wait(timeout=interval_s)
                continue
        rc, out, err = _psql_exec(
            namespace=namespace, pod=pod, user=user, db=db, password=password, sql=sql
        )
        if rc != 0:
            if not warned_exec:
                logger.warning(
                    "psql pg_stat_activity failed (pod=%s): %s",
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
                    if len(parts) < 3:
                        continue
                    writer.writerow([f"{ts_epoch:.3f}", ts, parts[0], parts[1], parts[2]])
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


def _capture_pg_stat_database(
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
        out_csv.write_text(_PG_STAT_DATABASE_HEADER, encoding="utf-8")
    pod: str | None = None
    warned_exec = False
    sql = (
        "SELECT numbackends, xact_commit, xact_rollback, blks_read, blks_hit, "
        "tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted, "
        "conflicts, deadlocks, temp_files, temp_bytes "
        "FROM pg_stat_database WHERE datname = $$" + db.replace("$$", "") + "$$;"
    )
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
            namespace=namespace, pod=pod, user=user, db=db, password=password, sql=sql
        )
        if rc != 0:
            if not warned_exec:
                logger.warning(
                    "psql pg_stat_database failed (pod=%s): %s",
                    pod,
                    (err or out).strip()[:200],
                )
                warned_exec = True
            pod = None
        else:
            line = out.strip().splitlines()[-1] if out.strip() else ""
            parts = [p.strip() for p in line.split(",")] if line else []
            if len(parts) >= 14:
                ts_epoch = time.time()
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                with open(out_csv, "a", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([f"{ts_epoch:.3f}", ts, *parts[:14]])
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


class PostgresMetricsCollector(DiagnosticsCollector):
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
                target=_capture_pg_stat_activity,
                kwargs={**shared, "out_csv": out_dir / "pg_stat_activity.csv"},
                daemon=True,
                name="diag-pg-activity",
            ),
            threading.Thread(
                target=_capture_pg_stat_database,
                kwargs={**shared, "out_csv": out_dir / "pg_stat_database.csv"},
                daemon=True,
                name="diag-pg-database",
            ),
        ]

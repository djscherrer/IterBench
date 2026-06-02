"""
Kubernetes per-run diagnostics: pod logs, events, restart counts, and
``pg_stat_*`` time series.

Sibling of :mod:`kubernetes` (``kubectl top`` sampling). Where ``kubectl top``
answers *how much* CPU/memory each pod is using, this logger answers *what is
happening to the pods and the database* during the run:

- ``stats/diagnostics/backend.log`` — streamed ``kubectl logs -f`` of all
  backend pods (prefixed with pod name and RFC3339 timestamps).
- ``stats/diagnostics/postgres.log`` — same for the primary postgres pod
  (when ``db_service_name`` is set).
- ``stats/diagnostics/pod_status.csv`` — periodic snapshot of pod phase,
  ready containers, restart counts, last termination reason.
- ``stats/diagnostics/events.jsonl`` — newly-seen ``kubectl get events``
  rows (de-duped by uid), one JSON object per line.
- ``stats/diagnostics/pg_stat_activity.csv`` — connection counts grouped by
  ``state``, plus oldest in-flight query age.
- ``stats/diagnostics/pg_stat_database.csv`` — single-row commit/rollback/
  blocks/deadlocks counters.
- ``stats/diagnostics/restart_logs/<pod>-previous.log`` — written at
  ``stop()`` for any pod whose restart count grew during the run.

All threads are best-effort: a single ``kubectl`` failure logs a warning and
loops; it never raises into the parent bench. The logger never blocks bench
shutdown — streaming ``kubectl logs`` subprocesses are SIGTERM'd then
SIGKILL'd after a short grace.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

from .base import UtilizationLogger, stats_root


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------


def _kubectl(args: Sequence[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
        check=False,
    )


def _diag_root(run_dir: Path) -> Path:
    d = stats_root(run_dir) / "diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spawn_kubectl(
    args: Sequence[str], *, stdout: int | object, stderr: int | object
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["kubectl", *args],
        stdout=stdout,
        stderr=stderr,
        env=os.environ.copy(),
        bufsize=0,
        # Put the child in its own process group so we can SIGTERM the whole
        # tree (kubectl spawns a watch helper) and so a SIGINT to the parent
        # bench doesn't take the streamer down before we can flush.
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )


def _terminate(proc: subprocess.Popen[bytes], *, grace_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------------------
# Streaming pod logs
# ---------------------------------------------------------------------------


def _stream_pod_logs(
    *,
    namespace: str,
    selector: str,
    out_file: Path,
    stop_event: threading.Event,
    logger: logging.Logger,
    max_log_requests: int,
    restart_backoff_s: float = 5.0,
) -> None:
    """
    Run ``kubectl logs -f -l <selector>`` and append everything to ``out_file``.

    Restarted by the loop until ``stop_event`` is set so that brief API server
    blips or scale-up events don't lose the stream. The header line written on
    each (re)start is what to grep for to see restarts in the log.
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    err_file = out_file.with_suffix(out_file.suffix + ".kubectl.stderr")
    while not stop_event.is_set():
        try:
            with open(out_file, "ab") as out, open(err_file, "ab") as err:
                header = (
                    f"\n# === kubectl logs -n {namespace} -l {selector} started at "
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n"
                ).encode()
                out.write(header)
                out.flush()
                proc = _spawn_kubectl(
                    [
                        "logs",
                        "-n",
                        namespace,
                        "-l",
                        selector,
                        "--all-containers=true",
                        "--prefix=true",
                        "--timestamps=true",
                        "--tail=-1",
                        f"--max-log-requests={max_log_requests}",
                        "--follow",
                    ],
                    stdout=out,
                    stderr=err,
                )
                while proc.poll() is None:
                    if stop_event.wait(timeout=1.0):
                        _terminate(proc)
                        return
        except Exception as exc:  # noqa: BLE001 — best-effort streamer
            logger.warning(
                "kubectl logs streamer failed (ns=%s sel=%s): %s; retrying",
                namespace,
                selector,
                exc,
            )
        # Don't busy-loop if kubectl is failing repeatedly.
        if stop_event.wait(timeout=restart_backoff_s):
            return


# ---------------------------------------------------------------------------
# Periodic pod status snapshots
# ---------------------------------------------------------------------------


_POD_STATUS_HEADER = (
    "ts_epoch_s,ts,pod,node,phase,ready,total,restart_count,"
    "last_termination_reason,last_termination_exit_code,deletion_ts\n"
)


def _capture_pod_status(
    *,
    namespace: str,
    out_csv: Path,
    stop_event: threading.Event,
    interval_s: int,
    logger: logging.Logger,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not out_csv.is_file() or out_csv.stat().st_size == 0:
        out_csv.write_text(_POD_STATUS_HEADER, encoding="utf-8")
    warned = False
    while not stop_event.is_set():
        loop_start = time.time()
        proc = _kubectl(
            ["get", "pods", "-n", namespace, "-o", "json"], timeout_s=45
        )
        if proc.returncode != 0:
            if not warned:
                logger.warning(
                    "kubectl get pods failed for diagnostics (ns=%s): %s",
                    namespace,
                    (proc.stderr or proc.stdout or "").strip()[:300],
                )
                warned = True
        else:
            try:
                data = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError as exc:
                logger.warning("pod status JSON parse failed: %s", exc)
                data = {}
            ts_epoch = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            rows: list[list[str]] = []
            for item in data.get("items") or []:
                meta = item.get("metadata") or {}
                status = item.get("status") or {}
                spec = item.get("spec") or {}
                pod = str(meta.get("name") or "")
                node = str(spec.get("nodeName") or "")
                phase = str(status.get("phase") or "")
                cs = status.get("containerStatuses") or []
                total = len(cs)
                ready = sum(1 for c in cs if c.get("ready"))
                restart_count = sum(int(c.get("restartCount") or 0) for c in cs)
                last_reason = ""
                last_exit = ""
                for c in cs:
                    last_state = (c.get("lastState") or {}).get("terminated") or {}
                    if last_state:
                        last_reason = str(last_state.get("reason") or "")
                        last_exit = str(last_state.get("exitCode") or "")
                        break
                deletion_ts = str(meta.get("deletionTimestamp") or "")
                rows.append(
                    [
                        f"{ts_epoch:.3f}",
                        ts,
                        pod,
                        node,
                        phase,
                        str(ready),
                        str(total),
                        str(restart_count),
                        last_reason,
                        last_exit,
                        deletion_ts,
                    ]
                )
            if rows:
                with open(out_csv, "a", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    for row in rows:
                        writer.writerow(row)
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


# ---------------------------------------------------------------------------
# Periodic event capture (de-duped by uid)
# ---------------------------------------------------------------------------


def _capture_events(
    *,
    namespace: str,
    out_jsonl: Path,
    stop_event: threading.Event,
    interval_s: int,
    logger: logging.Logger,
) -> None:
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    out_jsonl.touch()
    seen_uids: set[str] = set()
    warned = False
    while not stop_event.is_set():
        loop_start = time.time()
        proc = _kubectl(
            [
                "get",
                "events",
                "-n",
                namespace,
                "-o",
                "json",
                "--sort-by=.lastTimestamp",
            ],
            timeout_s=45,
        )
        if proc.returncode != 0:
            if not warned:
                logger.warning(
                    "kubectl get events failed for diagnostics (ns=%s): %s",
                    namespace,
                    (proc.stderr or proc.stdout or "").strip()[:300],
                )
                warned = True
        else:
            try:
                data = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError as exc:
                logger.warning("events JSON parse failed: %s", exc)
                data = {}
            new_rows: list[dict[str, object]] = []
            for item in data.get("items") or []:
                meta = item.get("metadata") or {}
                uid = str(meta.get("uid") or "")
                if not uid or uid in seen_uids:
                    continue
                seen_uids.add(uid)
                involved = item.get("involvedObject") or {}
                source = item.get("source") or {}
                new_rows.append(
                    {
                        "uid": uid,
                        "first_seen": item.get("firstTimestamp"),
                        "last_seen": item.get("lastTimestamp"),
                        "event_time": item.get("eventTime"),
                        "count": item.get("count"),
                        "type": item.get("type"),
                        "reason": item.get("reason"),
                        "message": item.get("message"),
                        "involved_kind": involved.get("kind"),
                        "involved_name": involved.get("name"),
                        "involved_namespace": involved.get("namespace"),
                        "source_component": source.get("component"),
                        "source_host": source.get("host"),
                    }
                )
            if new_rows:
                with open(out_jsonl, "a", encoding="utf-8") as f:
                    for row in new_rows:
                        f.write(json.dumps(row, sort_keys=True) + "\n")
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


# ---------------------------------------------------------------------------
# pg_stat_* sampling via ``kubectl exec``
# ---------------------------------------------------------------------------


_PG_STAT_ACTIVITY_HEADER = "ts_epoch_s,ts,state,count,max_age_s\n"
_PG_STAT_DATABASE_HEADER = (
    "ts_epoch_s,ts,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,"
    "tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,"
    "conflicts,deadlocks,temp_files,temp_bytes\n"
)


def _resolve_postgres_pod(
    *, namespace: str, label_selector: str, logger: logging.Logger
) -> str | None:
    proc = _kubectl(
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
    """Run a SQL query inside the postgres pod via ``kubectl exec``."""
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
            # Pod may have been rescheduled — drop the cached name and retry.
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


# ---------------------------------------------------------------------------
# Previous-container log capture (post-stop)
# ---------------------------------------------------------------------------


def _capture_previous_logs(
    *, namespace: str, selectors: Iterable[str], out_dir: Path, logger: logging.Logger
) -> None:
    """For every restarted pod, write the previous container's logs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for sel in selectors:
        proc = _kubectl(
            ["get", "pods", "-n", namespace, "-l", sel, "-o", "json"], timeout_s=45
        )
        if proc.returncode != 0:
            logger.warning(
                "previous-log capture: kubectl get pods failed (sel=%s): %s",
                sel,
                (proc.stderr or proc.stdout or "").strip()[:200],
            )
            continue
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            continue
        for item in data.get("items") or []:
            meta = item.get("metadata") or {}
            status = item.get("status") or {}
            pod = str(meta.get("name") or "")
            for c in status.get("containerStatuses") or []:
                rc = int(c.get("restartCount") or 0)
                cname = str(c.get("name") or "")
                if rc <= 0 or not pod or not cname:
                    continue
                logs = _kubectl(
                    [
                        "logs",
                        "-n",
                        namespace,
                        pod,
                        "-c",
                        cname,
                        "--previous",
                        "--timestamps=true",
                    ],
                    timeout_s=60,
                )
                if logs.returncode != 0:
                    continue
                out_file = out_dir / f"{pod}-{cname}-previous.log"
                out_file.write_text(logs.stdout or "", encoding="utf-8")
                logger.info(
                    "captured previous logs for restarted pod %s/%s -> %s",
                    pod,
                    cname,
                    out_file,
                )


# ---------------------------------------------------------------------------
# Public logger
# ---------------------------------------------------------------------------


class KubernetesDiagnosticsLogger(UtilizationLogger):
    """
    Bench-time diagnostics for a single iteration namespace.

    Always-on threads:

    - backend-log streamer (``app=backend``)
    - pod-status sampler
    - event sampler

    DB-only threads (skipped when ``db_service_name`` is falsy):

    - postgres-log streamer (``app=<service>``)
    - pg_stat_activity sampler
    - pg_stat_database sampler

    At ``stop()`` we also dump ``--previous`` logs for any restarted backend or
    postgres container into ``stats/diagnostics/restart_logs/``.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        namespace: str,
        db_service_name: str | None,
        db_user: str = "postgres",
        db_password: str = "postgres",
        db_name: str = "testdb",
        backend_label_selector: str = "app=backend",
        backend_max_log_requests: int = 50,
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._namespace = namespace.strip()
        self._db_service_name = (db_service_name or "").strip() or None
        self._db_user = db_user
        self._db_password = db_password
        self._db_name = db_name
        self._backend_label_selector = backend_label_selector
        self._backend_max_log_requests = backend_max_log_requests
        self._interval_s = interval_s

    def _postgres_selector(self) -> str | None:
        if not self._db_service_name:
            return None
        return f"app={self._db_service_name}"

    def _build_threads(self) -> list[threading.Thread]:
        diag = _diag_root(self._run_dir)
        threads: list[threading.Thread] = []
        threads.append(
            threading.Thread(
                target=_stream_pod_logs,
                kwargs={
                    "namespace": self._namespace,
                    "selector": self._backend_label_selector,
                    "out_file": diag / "backend.log",
                    "stop_event": self._stop_event,
                    "logger": self._log,
                    "max_log_requests": self._backend_max_log_requests,
                },
                daemon=True,
                name="k8s-diag-backend-logs",
            )
        )
        threads.append(
            threading.Thread(
                target=_capture_pod_status,
                kwargs={
                    "namespace": self._namespace,
                    "out_csv": diag / "pod_status.csv",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                },
                daemon=True,
                name="k8s-diag-pod-status",
            )
        )
        threads.append(
            threading.Thread(
                target=_capture_events,
                kwargs={
                    "namespace": self._namespace,
                    "out_jsonl": diag / "events.jsonl",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                },
                daemon=True,
                name="k8s-diag-events",
            )
        )
        pg_sel = self._postgres_selector()
        if pg_sel:
            threads.append(
                threading.Thread(
                    target=_stream_pod_logs,
                    kwargs={
                        "namespace": self._namespace,
                        "selector": pg_sel,
                        "out_file": diag / "postgres.log",
                        "stop_event": self._stop_event,
                        "logger": self._log,
                        "max_log_requests": 4,
                    },
                    daemon=True,
                    name="k8s-diag-postgres-logs",
                )
            )
            threads.append(
                threading.Thread(
                    target=_capture_pg_stat_activity,
                    kwargs={
                        "namespace": self._namespace,
                        "label_selector": pg_sel,
                        "user": self._db_user,
                        "password": self._db_password,
                        "db": self._db_name,
                        "out_csv": diag / "pg_stat_activity.csv",
                        "stop_event": self._stop_event,
                        "interval_s": self._interval_s,
                        "logger": self._log,
                    },
                    daemon=True,
                    name="k8s-diag-pg-activity",
                )
            )
            threads.append(
                threading.Thread(
                    target=_capture_pg_stat_database,
                    kwargs={
                        "namespace": self._namespace,
                        "label_selector": pg_sel,
                        "user": self._db_user,
                        "password": self._db_password,
                        "db": self._db_name,
                        "out_csv": diag / "pg_stat_database.csv",
                        "stop_event": self._stop_event,
                        "interval_s": self._interval_s,
                        "logger": self._log,
                    },
                    daemon=True,
                    name="k8s-diag-pg-database",
                )
            )
        return threads

    def stop(self) -> None:
        super().stop()
        # Best-effort: capture previous-container logs for any pod that
        # restarted during the run. Doing this after the streaming threads
        # have stopped (and the iteration is winding down) avoids racing
        # with the namespace cleanup that happens *after* the bench when
        # BAXBENCH_K8S_CLEANUP=both / after.
        try:
            selectors: list[str] = [self._backend_label_selector]
            pg_sel = self._postgres_selector()
            if pg_sel:
                selectors.append(pg_sel)
            _capture_previous_logs(
                namespace=self._namespace,
                selectors=selectors,
                out_dir=_diag_root(self._run_dir) / "restart_logs",
                logger=self._log,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("previous-log capture failed: %s", exc)

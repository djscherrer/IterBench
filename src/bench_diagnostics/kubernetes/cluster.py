"""Cluster-level Kubernetes diagnostics under ``diagnostics/kubernetes/cluster/``."""

from __future__ import annotations

import csv
import json
import logging
import threading
import time
from pathlib import Path
from typing import Sequence

from bench_diagnostics import _kubectl
from bench_diagnostics.base import DiagnosticsCollector
from bench_diagnostics.paths import kubernetes_cluster_dir


def _capture_kubectl_top_series(
    *,
    args: Sequence[str],
    out_csv: Path,
    header: str,
    stop_event: threading.Event,
    interval_s: int,
    logger: logging.Logger,
    parse_line,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8") as f:
        f.write(header)
    warned = False
    while not stop_event.is_set():
        loop_start = time.time()
        proc = _kubectl.run(list(args), timeout_s=45)
        if proc.returncode != 0:
            if not warned:
                logger.warning(
                    "kubectl %s failed (install metrics-server?): %s",
                    " ".join(args[:2]),
                    (proc.stderr or proc.stdout or "").strip()[:300],
                )
                warned = True
        else:
            ts_epoch = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(out_csv, "a", encoding="utf-8") as f:
                for line in (proc.stdout or "").strip().splitlines():
                    row = parse_line(ts_epoch, ts, line)
                    if row:
                        f.write(row)
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


def _parse_pod_line(ts_epoch: float, ts: str, line: str) -> str | None:
    parts = line.split()
    if len(parts) < 3:
        return None
    name, cpu, memory = parts[0], parts[1], parts[2]
    return f"{ts_epoch:.3f},{ts},{name},{cpu},{memory}\n"


def _parse_node_line(ts_epoch: float, ts: str, line: str) -> str | None:
    parts = line.split()
    if len(parts) < 3:
        return None
    name = parts[0]
    if len(parts) >= 5:
        cpu, cpu_pct, memory, memory_pct = parts[1], parts[2], parts[3], parts[4]
    else:
        cpu, cpu_pct, memory, memory_pct = parts[1], parts[2], "", ""
    return f"{ts_epoch:.3f},{ts},{name},{cpu},{cpu_pct},{memory},{memory_pct}\n"


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
        proc = _kubectl.run(
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
        proc = _kubectl.run(
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


class ClusterDiagnostics(DiagnosticsCollector):
    """``kubectl top`` series + pod status + events for one namespace."""

    def __init__(
        self,
        run_dir: Path,
        *,
        namespace: str,
        interval_s: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._namespace = namespace.strip()
        self._interval_s = interval_s

    def _build_threads(self) -> list[threading.Thread]:
        out_dir = kubernetes_cluster_dir(self._run_dir)
        return [
            threading.Thread(
                target=_capture_kubectl_top_series,
                kwargs={
                    "args": ("top", "pods", "-n", self._namespace, "--no-headers"),
                    "out_csv": out_dir / "kubectl_top_pods.csv",
                    "header": "ts_epoch_s,ts,pod,cpu,memory\n",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                    "parse_line": _parse_pod_line,
                },
                daemon=True,
                name="diag-top-pods",
            ),
            threading.Thread(
                target=_capture_kubectl_top_series,
                kwargs={
                    "args": ("top", "nodes", "--no-headers"),
                    "out_csv": out_dir / "kubectl_top_nodes.csv",
                    "header": "ts_epoch_s,ts,node,cpu,cpu_pct,memory,memory_pct\n",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                    "parse_line": _parse_node_line,
                },
                daemon=True,
                name="diag-top-nodes",
            ),
            threading.Thread(
                target=_capture_pod_status,
                kwargs={
                    "namespace": self._namespace,
                    "out_csv": out_dir / "pod_status.csv",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                },
                daemon=True,
                name="diag-pod-status",
            ),
            threading.Thread(
                target=_capture_events,
                kwargs={
                    "namespace": self._namespace,
                    "out_jsonl": out_dir / "events.jsonl",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                },
                daemon=True,
                name="diag-events",
            ),
        ]

"""Kubernetes pod/node utilization via ``kubectl top`` (metrics-server)."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence

from .base import UtilizationLogger, stats_root


def _kubectl(args: Sequence[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
        check=False,
    )


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
        proc = _kubectl(list(args), timeout_s=45)
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
            lines = (proc.stdout or "").strip().splitlines()
            with open(out_csv, "a", encoding="utf-8") as f:
                for line in lines:
                    row = parse_line(ts_epoch, ts, line)
                    if row:
                        f.write(row)
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


def _parse_pod_line(ts_epoch: float, ts: str, line: str) -> str | None:
    """``kubectl top pods``: NAME, CPU (e.g. 80m), MEMORY (e.g. 809Mi)."""
    parts = line.split()
    if len(parts) < 3:
        return None
    name, cpu, memory = parts[0], parts[1], parts[2]
    return f"{ts_epoch:.3f},{ts},{name},{cpu},{memory}\n"


def _parse_node_line(ts_epoch: float, ts: str, line: str) -> str | None:
    """``kubectl top nodes``: NAME, CPU, CPU%, MEMORY, MEMORY%."""
    parts = line.split()
    if len(parts) < 3:
        return None
    name = parts[0]
    if len(parts) >= 5:
        cpu, cpu_pct, memory, memory_pct = parts[1], parts[2], parts[3], parts[4]
    else:
        # Older BaxBench CSVs only stored CPU + CPU%; keep parsing compatible.
        cpu, cpu_pct, memory, memory_pct = parts[1], parts[2], "", ""
    return (
        f"{ts_epoch:.3f},{ts},{name},{cpu},{cpu_pct},{memory},{memory_pct}\n"
    )


class KubernetesUtilizationLogger(UtilizationLogger):
    """Poll ``kubectl top pods`` and ``kubectl top nodes`` into ``stats/kubernetes/``."""

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
        k8s_dir = stats_root(self._run_dir) / "kubernetes"
        k8s_dir.mkdir(parents=True, exist_ok=True)
        pod_csv = k8s_dir / "pod_top.csv"
        node_csv = k8s_dir / "node_top.csv"
        return [
            threading.Thread(
                target=_capture_kubectl_top_series,
                kwargs={
                    "args": ("top", "pods", "-n", self._namespace, "--no-headers"),
                    "out_csv": pod_csv,
                    "header": "ts_epoch_s,ts,pod,cpu,memory\n",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                    "parse_line": _parse_pod_line,
                },
                daemon=True,
                name="k8s-top-pods",
            ),
            threading.Thread(
                target=_capture_kubectl_top_series,
                kwargs={
                    "args": ("top", "nodes", "--no-headers"),
                    "out_csv": node_csv,
                    "header": "ts_epoch_s,ts,node,cpu,cpu_pct,memory,memory_pct\n",
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                    "parse_line": _parse_node_line,
                },
                daemon=True,
                name="k8s-top-nodes",
            ),
        ]

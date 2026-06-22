"""Redis ``INFO`` sampling under ``diagnostics/kubernetes/cache/``."""

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
from bench_diagnostics.paths import kubernetes_cache_dir

_REDIS_INFO_HEADER = (
    "ts_epoch_s,ts,pod,used_memory_bytes,used_memory_peak_bytes,"
    "connected_clients,keyspace_hits,keyspace_misses,evicted_keys,"
    "instantaneous_ops_per_sec,db_keys\n"
)


def _resolve_redis_pods(*, namespace: str, logger: logging.Logger) -> list[str]:
    proc = _kubectl.run(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "baxbench.dev/role=cache",
            "-o",
            "jsonpath={.items[?(@.status.phase==\"Running\")].metadata.name}",
        ],
        timeout_s=30,
    )
    if proc.returncode != 0:
        logger.warning(
            "could not resolve redis pods (ns=%s): %s",
            namespace,
            (proc.stderr or proc.stdout or "").strip()[:200],
        )
        return []
    return [n for n in (proc.stdout or "").split() if n]


def _parse_info(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def _db_keys_from_info(info: dict[str, str]) -> int:
    total = 0
    for key, value in info.items():
        if not key.startswith("db"):
            continue
        for part in value.split(","):
            part = part.strip()
            if part.startswith("keys="):
                try:
                    total += int(part.split("=", 1)[1])
                except ValueError:
                    pass
    return total


def _redis_info(
    *, namespace: str, pod: str, timeout_s: int = 15
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            pod,
            "--",
            "redis-cli",
            "INFO",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _capture_redis_info(
    *,
    namespace: str,
    out_csv: Path,
    stop_event: threading.Event,
    interval_s: int,
    logger: logging.Logger,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not out_csv.is_file() or out_csv.stat().st_size == 0:
        out_csv.write_text(_REDIS_INFO_HEADER, encoding="utf-8")
    warned_exec = False
    while not stop_event.is_set():
        loop_start = time.time()
        pods = _resolve_redis_pods(namespace=namespace, logger=logger)
        if not pods:
            stop_event.wait(timeout=interval_s)
            continue
        ts_epoch = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(out_csv, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for pod in pods:
                rc, out, err = _redis_info(namespace=namespace, pod=pod)
                if rc != 0:
                    if not warned_exec:
                        logger.warning(
                            "redis INFO failed (pod=%s): %s",
                            pod,
                            (err or out).strip()[:200],
                        )
                        warned_exec = True
                    continue
                info = _parse_info(out)
                writer.writerow(
                    [
                        f"{ts_epoch:.3f}",
                        ts,
                        pod,
                        info.get("used_memory", ""),
                        info.get("used_memory_peak", ""),
                        info.get("connected_clients", ""),
                        info.get("keyspace_hits", ""),
                        info.get("keyspace_misses", ""),
                        info.get("evicted_keys", ""),
                        info.get("instantaneous_ops_per_sec", ""),
                        _db_keys_from_info(info),
                    ]
                )
        elapsed = time.time() - loop_start
        stop_event.wait(timeout=max(0.5, interval_s - elapsed))


class RedisMetricsCollector(DiagnosticsCollector):
    """Sample ``redis-cli INFO`` from cache pods when present."""

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
        out_csv = kubernetes_cache_dir(self._run_dir) / "redis_info.csv"
        return [
            threading.Thread(
                target=_capture_redis_info,
                kwargs={
                    "namespace": self._namespace,
                    "out_csv": out_csv,
                    "stop_event": self._stop_event,
                    "interval_s": self._interval_s,
                    "logger": self._log,
                },
                daemon=True,
                name="diag-redis-info",
            )
        ]

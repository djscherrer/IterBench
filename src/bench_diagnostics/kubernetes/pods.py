"""Streaming container-log capture under ``diagnostics/kubernetes/logs/``."""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from bench_diagnostics import _kubectl
from bench_diagnostics.base import DiagnosticsCollector
from bench_diagnostics.paths import kubernetes_logs_dir, kubernetes_logs_restarts_dir


@dataclass(frozen=True)
class PodLogStream:
    name: str
    selector: str
    max_log_requests: int = 50


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
    out_file.parent.mkdir(parents=True, exist_ok=True)
    err_file = out_file.with_suffix(out_file.suffix + ".kubectl.stderr")
    while not stop_event.is_set():
        try:
            err_buffer = io.BytesIO()
            with open(out_file, "ab") as out:
                header = (
                    f"\n# === kubectl logs -n {namespace} -l {selector} started at "
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n"
                ).encode()
                out.write(header)
                out.flush()
                proc = _kubectl.spawn(
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
                    stderr=err_buffer,
                )
                while proc.poll() is None:
                    if stop_event.wait(timeout=1.0):
                        _kubectl.terminate(proc)
                        break
            err_data = err_buffer.getvalue()
            if err_data:
                err_file.parent.mkdir(parents=True, exist_ok=True)
                with open(err_file, "ab") as err:
                    err.write(err_data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "kubectl logs streamer failed (ns=%s sel=%s): %s; retrying",
                namespace,
                selector,
                exc,
            )
        if stop_event.wait(timeout=restart_backoff_s):
            return


def _capture_previous_logs(
    *, namespace: str, selectors: Iterable[str], out_dir: Path, logger: logging.Logger
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for sel in selectors:
        proc = _kubectl.run(
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
                logs = _kubectl.run(
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


class PodLogsCollector(DiagnosticsCollector):
    def __init__(
        self,
        run_dir: Path,
        *,
        namespace: str,
        streams: Sequence[PodLogStream],
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._run_dir = run_dir
        self._namespace = namespace.strip()
        self._streams = tuple(streams)

    def _build_threads(self) -> list[threading.Thread]:
        out_dir = kubernetes_logs_dir(self._run_dir)
        threads: list[threading.Thread] = []
        for stream in self._streams:
            threads.append(
                threading.Thread(
                    target=_stream_pod_logs,
                    kwargs={
                        "namespace": self._namespace,
                        "selector": stream.selector,
                        "out_file": out_dir / f"{stream.name}.log",
                        "stop_event": self._stop_event,
                        "logger": self._log,
                        "max_log_requests": stream.max_log_requests,
                    },
                    daemon=True,
                    name=f"diag-pod-logs-{stream.name}",
                )
            )
        return threads

    def stop(self) -> None:
        super().stop()
        out_dir = kubernetes_logs_dir(self._run_dir)
        for stream in self._streams:
            err_file = out_dir / f"{stream.name}.log.kubectl.stderr"
            try:
                if err_file.is_file() and err_file.stat().st_size == 0:
                    err_file.unlink()
            except OSError:
                pass
        try:
            _capture_previous_logs(
                namespace=self._namespace,
                selectors=[s.selector for s in self._streams],
                out_dir=kubernetes_logs_restarts_dir(self._run_dir),
                logger=self._log,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("previous-log capture failed: %s", exc)

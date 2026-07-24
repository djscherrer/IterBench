"""Timestamp parsing and load-boundary detection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

_POD_LINE_RE = re.compile(
    r"^\[pod/(?P<pod>[^/]+)/(?P<container>[^\]]+)\]\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)\s+(?P<msg>.*)$"
)

_LOAD_START_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d+)\].*All users spawned",
    re.IGNORECASE,
)


def parse_pod_log_line(line: str) -> tuple[str, str, float, str] | None:
    """Return ``(pod, container, epoch_s, message)`` for a kubectl log line."""
    m = _POD_LINE_RE.match(line.rstrip())
    if not m:
        return None
    pod = m.group("pod")
    container = m.group("container")
    raw_ts = m.group("ts")
    try:
        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
    except ValueError:
        return None
    return pod, container, epoch, m.group("msg")


def infer_load_start_epoch_s(
    bench_log: str,
    *,
    tz: timezone | None = None,
) -> float | None:
    """
    First Locust ``All users spawned`` timestamp in ``bench.log`` (epoch seconds).

    ``tz`` should match pod log timestamps (take from the first pod log line).
    Without ``tz``, timestamps are interpreted as UTC.
    """
    m = _LOAD_START_RE.search(bench_log)
    if not m:
        return None
    date_part, ms_part = m.group(1), m.group(2)
    try:
        dt = datetime.strptime(date_part, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(
            microsecond=int(ms_part) * 1000,
            tzinfo=tz or timezone.utc,
        )
        return dt.timestamp()
    except ValueError:
        return None


def timezone_from_pod_log(path: Path) -> timezone | None:
    """Read the first parseable pod log line and return its timezone."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                m = _POD_LINE_RE.match(raw.rstrip())
                if not m:
                    continue
                raw_ts = m.group("ts")
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return dt.tzinfo
    except OSError:
        return None
    return None


def format_epoch_label(epoch_s: float | None) -> str:
    if epoch_s is None:
        return "?"
    dt = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_run_elapsed_s(epoch_s: float | None, *, load_start_epoch: float | None) -> str:
    """Seconds since load start (matches adaptive ramp ``t (s)``)."""
    if epoch_s is None or load_start_epoch is None:
        return "?"
    return f"{max(0.0, epoch_s - load_start_epoch):.0f}"

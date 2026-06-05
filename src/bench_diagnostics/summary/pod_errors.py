"""
Aggregate pod log lines into error classes with pre-load / under-load timing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from datetime import timezone
from typing import Iterable

from ..paths import kubernetes_pods_dir
from ._time import (
    format_epoch_label,
    infer_load_start_epoch_s,
    parse_pod_log_line,
    timezone_from_pod_log,
)

# Volatile fragments to collapse duplicate classes across pods/IPs.
_NORMALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<host>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<id>"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
    (re.compile(r"at \S+"), "at <frame>"),
    (re.compile(r"\s+"), " "),
]

_CLASS_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("db_connection_refused", re.compile(r"ECONNREFUSED", re.I)),
    ("db_deadlock", re.compile(r"deadlock detected", re.I)),
    ("db_missing_relation", re.compile(r'relation "[^"]+" does not exist', re.I)),
    ("db_pool_exhausted", re.compile(r"(too many clients|remaining connection slots)", re.I)),
    (
        "db_connection_dropped",
        re.compile(r"(ETIMEDOUT|ECONNRESET|Connection terminated)", re.I),
    ),
    (
        "db_schema_race",
        re.compile(r"duplicate key value violates unique constraint", re.I),
    ),
    ("app_startup_failure", re.compile(r"Failed to start server", re.I)),
]

_SKIP_MSG_RE = re.compile(
    r"^(at |\^|code:|errno:|syscall:|\{|\}|  File |"
    r"(file|line|routine|severity|detail|hint|position|internal|schema|table|column|dataType|constraint):)",
    re.I,
)

_POSTGRES_PRIMARY_RE = re.compile(r"\b(ERROR|FATAL):\s+", re.I)

_CLASS_LABELS: dict[str, str] = {
    "db_connection_refused": "Database connection refused",
    "db_deadlock": "PostgreSQL deadlock detected",
    "db_missing_relation": "Missing database relation",
    "db_pool_exhausted": "PostgreSQL connection limit reached",
    "db_connection_dropped": "Database connection dropped",
    "app_startup_failure": "Application failed to start",
    "db_schema_race": "Schema initialization race (duplicate catalog object)",
}


@dataclass
class ErrorClassRow:
    class_id: str
    label: str
    source: str
    count: int
    pod_count: int
    pre_load_count: int
    under_load_count: int
    first_seen_epoch: float
    last_seen_epoch: float
    example_line: str
    source_counts: tuple[tuple[str, int], ...] = ()

    @property
    def first_seen(self) -> str:
        return format_epoch_label(self.first_seen_epoch)

    @property
    def last_seen(self) -> str:
        return format_epoch_label(self.last_seen_epoch)

    @property
    def source_breakdown(self) -> str:
        """``backend 1057, postgres 1057`` (the same rejection is logged on
        both sides, so the per-source split shows whether the total is inflated
        by duplicate client+server records of one event)."""
        if not self.source_counts:
            return self.source
        return ", ".join(f"{src} {cnt}" for src, cnt in self.source_counts)


@dataclass
class PodErrorSummary:
    rows: tuple[ErrorClassRow, ...] = ()
    load_start_epoch: float | None = None
    sources_scanned: tuple[str, ...] = ()
    lines_scanned: int = 0

    def to_prompt_block(self) -> str:
        if not self.rows:
            scanned = ", ".join(self.sources_scanned) or "(none)"
            return (
                f"(no application/database errors detected in pod logs; "
                f"scanned {self.lines_scanned} lines from {scanned})"
            )

        boundary = format_epoch_label(self.load_start_epoch)
        lines = [
            (
                "Errors grouped by class from pod logs "
                f"(load boundary: first Locust spawn at {boundary})."
            ),
            "",
            "| Class | Source (count) | Total | Pods | Pre-load | Under load | First | Last |",
            "|---|---|---:|---:|---:|---:|---|---|",
        ]
        for row in self.rows:
            lines.append(
                f"| `{row.class_id}` | {row.source_breakdown} | {row.count} | "
                f"{row.pod_count} | {row.pre_load_count} | {row.under_load_count} | "
                f"{row.first_seen} | {row.last_seen} |"
            )
        lines.append("")
        lines.append("**Representative line per class** (stack traces omitted):")
        for row in self.rows:
            example = row.example_line.strip()
            if len(example) > 200:
                example = example[:197] + "…"
            lines.append(f"- `{row.class_id}`: `{example}`")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "load_start_epoch": self.load_start_epoch,
            "sources_scanned": list(self.sources_scanned),
            "lines_scanned": self.lines_scanned,
            "rows": [
                {
                    "class_id": r.class_id,
                    "label": r.label,
                    "source": r.source,
                    "count": r.count,
                    "pod_count": r.pod_count,
                    "pre_load_count": r.pre_load_count,
                    "under_load_count": r.under_load_count,
                    "first_seen_epoch": r.first_seen_epoch,
                    "last_seen_epoch": r.last_seen_epoch,
                    "example_line": r.example_line,
                    "source_counts": [list(sc) for sc in r.source_counts],
                }
                for r in self.rows
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PodErrorSummary:
        rows = tuple(
            ErrorClassRow(
                class_id=str(r["class_id"]),
                label=str(r.get("label", r["class_id"])),
                source=str(r["source"]),
                count=int(r["count"]),
                pod_count=int(r["pod_count"]),
                pre_load_count=int(r["pre_load_count"]),
                under_load_count=int(r["under_load_count"]),
                first_seen_epoch=float(r["first_seen_epoch"]),
                last_seen_epoch=float(r["last_seen_epoch"]),
                example_line=str(r["example_line"]),
                source_counts=tuple(
                    (str(s), int(c)) for s, c in (r.get("source_counts") or [])
                ),
            )
            for r in data.get("rows") or []
        )
        return cls(
            rows=rows,
            load_start_epoch=data.get("load_start_epoch"),
            sources_scanned=tuple(data.get("sources_scanned") or ()),
            lines_scanned=int(data.get("lines_scanned") or 0),
        )


def _normalize_message(msg: str) -> str:
    out = msg.strip()
    for pat, repl in _NORMALIZE_PATTERNS:
        out = pat.sub(repl, out)
    return out.strip()[:200]


def _classify_message(msg: str) -> tuple[str, str]:
    for class_id, pat in _CLASS_RULES:
        if pat.search(msg):
            return class_id, pat.pattern
    norm = _normalize_message(msg)
    return f"other:{norm[:80]}", norm


def _extract_error_message(msg: str, *, source: str) -> str | None:
    stripped = msg.strip()
    if not stripped or _SKIP_MSG_RE.match(stripped):
        return None
    if source == "postgres":
        if not _POSTGRES_PRIMARY_RE.search(stripped):
            return None
        m = re.search(
            r"(?:ERROR|FATAL):\s+(.+?)(?:\s+at character \d+)?$",
            stripped,
            re.I,
        )
        return m.group(1).strip() if m else None
    if re.search(r"\b(ERROR|FATAL):\s+", stripped, re.I):
        m = re.search(r"(?:ERROR|FATAL):\s+(.+)$", stripped, re.I)
        return m.group(1).strip() if m else stripped
    if re.search(
        r"(Error:|Failed to|ECONNREFUSED|ETIMEDOUT|ECONNRESET|deadlock|exception)",
        stripped,
        re.I,
    ):
        return stripped
    return None


def _iter_log_paths(run_dir: Path) -> Iterable[tuple[str, Path]]:
    pods_dir = kubernetes_pods_dir(run_dir)
    for name, source in (("backend.log", "backend"), ("postgres.log", "postgres")):
        path = pods_dir / name
        if path.is_file():
            yield source, path


def summarize_pod_errors(
    run_dir: Path,
    *,
    bench_log: str = "",
    max_classes: int = 12,
) -> PodErrorSummary:
    log_tz: timezone | None = None
    for _source, path in _iter_log_paths(run_dir):
        log_tz = timezone_from_pod_log(path)
        if log_tz is not None:
            break

    load_start = (
        infer_load_start_epoch_s(bench_log, tz=log_tz) if bench_log else None
    )

    @dataclass
    class _Acc:
        count: int = 0
        pods: set[str] = field(default_factory=set)
        pre_load: int = 0
        under_load: int = 0
        first_epoch: float = 0.0
        last_epoch: float = 0.0
        example: str = ""
        source_counts: dict[str, int] = field(default_factory=dict)

    by_key: dict[str, _Acc] = defaultdict(_Acc)
    labels_by_class: dict[str, str] = {}
    sources: list[str] = []
    lines_scanned = 0

    for source, path in _iter_log_paths(run_dir):
        sources.append(path.name)
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for raw in f:
                    lines_scanned += 1
                    parsed = parse_pod_log_line(raw)
                    if parsed is None:
                        continue
                    pod, _container, epoch, msg = parsed
                    err_msg = _extract_error_message(msg, source=source)
                    if err_msg is None:
                        continue
                    class_id, label = _classify_message(err_msg)
                    labels_by_class[class_id] = _CLASS_LABELS.get(class_id, label)
                    acc = by_key[class_id]
                    acc.count += 1
                    acc.source_counts[source] = acc.source_counts.get(source, 0) + 1
                    acc.pods.add(pod)
                    if load_start is not None and epoch < load_start:
                        acc.pre_load += 1
                    else:
                        acc.under_load += 1
                    if acc.count == 1:
                        acc.first_epoch = epoch
                        acc.example = raw.rstrip()[:400]
                    acc.last_epoch = epoch
        except OSError:
            continue

    rows: list[ErrorClassRow] = []
    for class_id, acc in sorted(
        by_key.items(), key=lambda kv: kv[1].count, reverse=True
    )[:max_classes]:
        source_counts = tuple(
            sorted(acc.source_counts.items(), key=lambda kv: kv[1], reverse=True)
        )
        source = "+".join(src for src, _ in source_counts)
        rows.append(
            ErrorClassRow(
                class_id=class_id,
                label=labels_by_class.get(class_id, class_id),
                source=source,
                count=acc.count,
                pod_count=len(acc.pods),
                pre_load_count=acc.pre_load,
                under_load_count=acc.under_load,
                first_seen_epoch=acc.first_epoch,
                last_seen_epoch=acc.last_epoch,
                example_line=acc.example,
                source_counts=source_counts,
            )
        )

    return PodErrorSummary(
        rows=tuple(rows),
        load_start_epoch=load_start,
        sources_scanned=tuple(sources),
        lines_scanned=lines_scanned,
    )

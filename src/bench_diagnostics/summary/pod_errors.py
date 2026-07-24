"""
Aggregate pod log lines into per-source counts, warning/error classes, and timing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from datetime import timezone
from typing import Iterable

from ..paths import resolve_kubernetes_logs_dir
from ._time import (
    format_run_elapsed_s,
    infer_load_start_epoch_s,
    parse_pod_log_line,
    timezone_from_pod_log,
)

# Volatile fragments to collapse duplicate classes across pods/IPs.
_NORMALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<host>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<id>"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
    # Collapse driver-generated prepared statement names (s13, s245, ...).
    (re.compile(r'prepared statement "s\d+"', re.I), 'prepared statement "s<n>"'),
    (re.compile(r"at \S+"), "at <frame>"),
    (re.compile(r"\s+"), " "),
]

_CLASS_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("db_connection_refused", re.compile(r"ECONNREFUSED", re.I)),
    ("db_deadlock", re.compile(r"deadlock detected", re.I)),
    ("db_missing_relation", re.compile(r'relation "[^"]+" does not exist', re.I)),
    ("db_pool_exhausted", re.compile(r"(too many clients|remaining connection slots)", re.I)),
    (
        "db_prepared_statement_missing",
        re.compile(r'prepared statement "s\d+" does not exist', re.I),
    ),
    (
        "db_prepared_statement_exists",
        re.compile(r'prepared statement "s\d+" already exists', re.I),
    ),
    (
        "db_protocol_desync",
        re.compile(r"insufficient data left in message", re.I),
    ),
    (
        "db_invalid_utf8",
        re.compile(r'invalid byte sequence for encoding "UTF8"', re.I),
    ),
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

_POSTGRES_LEVEL_RE = re.compile(r"\b(LOG|WARNING|ERROR|FATAL):\s+", re.I)
_REDIS_WARNING_RE = re.compile(r"^\s*#\s*Warning", re.I)
_REDIS_ERROR_RE = re.compile(r"^\s*#\s*Error", re.I)

_CLASS_LABELS: dict[str, str] = {
    "db_connection_refused": "Database connection refused",
    "db_deadlock": "PostgreSQL deadlock detected",
    "db_missing_relation": "Missing database relation",
    "db_pool_exhausted": "PostgreSQL connection limit reached",
    "db_connection_dropped": "Database connection dropped",
    "app_startup_failure": "Application failed to start",
    "db_schema_race": "Schema initialization race (duplicate catalog object)",
    "db_prepared_statement_missing": "Prepared statement missing (pooler/protocol mismatch)",
    "db_prepared_statement_exists": "Prepared statement already exists (pooler/protocol mismatch)",
    "db_protocol_desync": "PostgreSQL protocol desynchronization",
    "db_invalid_utf8": "Invalid UTF-8 payload reaching PostgreSQL",
}

_LOG_LINE_PREFIX_RE = re.compile(
    r"^\[pod/[^/]+/[^\]]+\]\s+\d{4}-\d{2}-\d{2}T[\d:.+-]+(?:Z|[+-]\d{2}:\d{2})?\s+"
)
_PG_LOG_MSG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} [\d:.]+ GMT \[\d+\] (?:LOG|WARNING|ERROR|FATAL|PANIC):\s+",
    re.I,
)


def _shorten_example_line(raw: str) -> str:
    line = _LOG_LINE_PREFIX_RE.sub("", raw.strip())
    line = _PG_LOG_MSG_RE.sub("", line)
    if len(line) > 160:
        return line[:157] + "…"
    return line


_LOG_SOURCES: tuple[tuple[str, str], ...] = (
    ("backend.log", "backend"),
    ("postgres.log", "postgres"),
    ("postgres-replica.log", "postgres-replica"),
    ("pgbouncer.log", "pgbouncer"),
    ("pgbouncer-read.log", "pgbouncer-read"),
    ("redis.log", "redis"),
)


@dataclass
class LogSourceStats:
    source: str
    lines_total: int = 0
    warnings: int = 0
    errors: int = 0
    pre_load_errors: int = 0
    under_load_errors: int = 0


@dataclass
class LogClassRow:
    class_id: str
    label: str
    severity: str
    source: str
    count: int
    pod_count: int
    pre_load_count: int
    under_load_count: int
    first_seen_epoch: float
    last_seen_epoch: float
    example_line: str
    source_counts: tuple[tuple[str, int], ...] = ()
    load_start_epoch: float | None = None

    @property
    def first_seen(self) -> str:
        return format_run_elapsed_s(
            self.first_seen_epoch, load_start_epoch=self.load_start_epoch
        )

    @property
    def last_seen(self) -> str:
        return format_run_elapsed_s(
            self.last_seen_epoch, load_start_epoch=self.load_start_epoch
        )

    @property
    def source_breakdown(self) -> str:
        if not self.source_counts:
            return self.source
        return ", ".join(f"{src} {cnt}" for src, cnt in self.source_counts)


@dataclass
class PodErrorSummary:
    sources: tuple[LogSourceStats, ...] = ()
    error_rows: tuple[LogClassRow, ...] = ()
    warning_rows: tuple[LogClassRow, ...] = ()
    load_start_epoch: float | None = None
    sources_scanned: tuple[str, ...] = ()
    lines_scanned: int = 0

    def to_prompt_block(self) -> str:
        lines: list[str] = []
        if self.sources:
            lines.extend(
                [
                    "Per-source log counts:",
                    "",
                    "| Source | Lines | Warnings | Errors | Pre-load errors | Under-load errors |",
                    "|---|---:|---:|---:|---:|---:|",
                ]
            )
            for st in self.sources:
                lines.append(
                    f"| {st.source} | {st.lines_total} | {st.warnings} | {st.errors} | "
                    f"{st.pre_load_errors} | {st.under_load_errors} |"
                )
            lines.append("")

        if not self.error_rows and not self.warning_rows:
            scanned = ", ".join(self.sources_scanned) or "(none)"
            lines.append(
                f"(no warnings or errors detected in pod logs; "
                f"scanned {self.lines_scanned} lines from {scanned})"
            )
            return "\n".join(lines).rstrip()

        if self.error_rows:
            lines.extend(
                [
                    f"**Errors** (t=0 at first Locust spawn; times are seconds into run):",
                    "",
                    "| Class | Log source | Count | Pods | Pre-load | Under load | First t (s) | Last t (s) |",
                    "|---|---|---:|---:|---:|---:|---:|---:|",
                ]
            )
            for row in self.error_rows:
                src = row.source_counts[0][0] if len(row.source_counts) == 1 else row.source
                lines.append(
                    f"| `{row.class_id}` | {src} | {row.count} | "
                    f"{row.pod_count} | {row.pre_load_count} | {row.under_load_count} | "
                    f"{row.first_seen} | {row.last_seen} |"
                )
            lines.append("")
            example_limit = 3
            lines.append(
                f"Example per class (top {min(example_limit, len(self.error_rows))} by count):"
            )
            for row in self.error_rows[:example_limit]:
                lines.append(f"- `{row.class_id}`: `{_shorten_example_line(row.example_line)}`")
            if len(self.error_rows) > example_limit:
                lines.append(
                    f"- (examples omitted for {len(self.error_rows) - example_limit} smaller classes)"
                )
            lines.append("")

        if self.warning_rows:
            lines.extend(
                [
                    "**Warnings** (times are seconds into run; t=0 at first Locust spawn):",
                    "",
                    "| Class | Log source | Count | Pods | First t (s) | Last t (s) |",
                    "|---|---|---:|---:|---:|---:|",
                ]
            )
            for row in self.warning_rows:
                src = row.source_counts[0][0] if len(row.source_counts) == 1 else row.source
                lines.append(
                    f"| `{row.class_id}` | {src} | {row.count} | "
                    f"{row.pod_count} | {row.first_seen} | {row.last_seen} |"
                )
            lines.append("")
            example_limit = 3
            lines.append(
                f"Example per class (top {min(example_limit, len(self.warning_rows))} by count):"
            )
            for row in self.warning_rows[:example_limit]:
                lines.append(f"- `{row.class_id}`: `{_shorten_example_line(row.example_line)}`")
            if len(self.warning_rows) > example_limit:
                lines.append(
                    f"- (examples omitted for {len(self.warning_rows) - example_limit} smaller classes)"
                )

        return "\n".join(lines).rstrip()

    def to_dict(self) -> dict:
        def _row_dict(r: LogClassRow) -> dict:
            return {
                "class_id": r.class_id,
                "label": r.label,
                "severity": r.severity,
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

        return {
            "load_start_epoch": self.load_start_epoch,
            "sources_scanned": list(self.sources_scanned),
            "lines_scanned": self.lines_scanned,
            "sources": [
                {
                    "source": s.source,
                    "lines_total": s.lines_total,
                    "warnings": s.warnings,
                    "errors": s.errors,
                    "pre_load_errors": s.pre_load_errors,
                    "under_load_errors": s.under_load_errors,
                }
                for s in self.sources
            ],
            "error_rows": [_row_dict(r) for r in self.error_rows],
            "warning_rows": [_row_dict(r) for r in self.warning_rows],
            # Legacy alias for older consumers.
            "rows": [_row_dict(r) for r in self.error_rows],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PodErrorSummary:
        def _parse_rows(items: list, default_severity: str) -> tuple[LogClassRow, ...]:
            return tuple(
                LogClassRow(
                    class_id=str(r["class_id"]),
                    label=str(r.get("label", r["class_id"])),
                    severity=str(r.get("severity", default_severity)),
                    source=str(r["source"]),
                    count=int(r["count"]),
                    pod_count=int(r["pod_count"]),
                    pre_load_count=int(r.get("pre_load_count") or 0),
                    under_load_count=int(r.get("under_load_count") or 0),
                    first_seen_epoch=float(r["first_seen_epoch"]),
                    last_seen_epoch=float(r["last_seen_epoch"]),
                    example_line=str(r["example_line"]),
                    source_counts=tuple(
                        (str(s), int(c)) for s, c in (r.get("source_counts") or [])
                    ),
                )
                for r in items
            )

        sources = tuple(
            LogSourceStats(
                source=str(s["source"]),
                lines_total=int(s.get("lines_total") or 0),
                warnings=int(s.get("warnings") or 0),
                errors=int(s.get("errors") or 0),
                pre_load_errors=int(s.get("pre_load_errors") or 0),
                under_load_errors=int(s.get("under_load_errors") or 0),
            )
            for s in data.get("sources") or []
        )
        error_rows = _parse_rows(data.get("error_rows") or data.get("rows") or [], "error")
        warning_rows = _parse_rows(data.get("warning_rows") or [], "warning")
        return cls(
            sources=sources,
            error_rows=error_rows,
            warning_rows=warning_rows,
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


def _extract_postgres_message(msg: str, level: str) -> str | None:
    m = re.search(
        rf"(?:{level}):\s+(.+?)(?:\s+at character \d+)?$",
        msg.strip(),
        re.I,
    )
    return m.group(1).strip() if m else msg.strip()


def _classify_line(msg: str, *, source: str) -> tuple[str, str] | None:
    """Return (severity, message) or None if info/noise."""
    stripped = msg.strip()
    if not stripped or stripped.startswith("# === kubectl logs"):
        return None
    if _SKIP_MSG_RE.match(stripped):
        return None

    if source in {"postgres", "postgres-replica"}:
        m = _POSTGRES_LEVEL_RE.search(stripped)
        if not m:
            return None
        level = m.group(1).upper()
        body = _extract_postgres_message(stripped, level)
        if level in {"ERROR", "FATAL"}:
            return "error", body or stripped
        if level == "WARNING":
            return "warning", body or stripped
        return None

    if source == "redis":
        if _REDIS_ERROR_RE.search(stripped):
            return "error", stripped
        if _REDIS_WARNING_RE.search(stripped):
            return "warning", stripped
        return None

    if re.search(r"\b(ERROR|FATAL):\s+", stripped, re.I):
        m = re.search(r"(?:ERROR|FATAL):\s+(.+)$", stripped, re.I)
        return "error", (m.group(1).strip() if m else stripped)

    if re.search(r"\b(WARNING|WARN)\b", stripped, re.I):
        return "warning", stripped

    if re.search(
        r"(Error:|Failed to|ECONNREFUSED|ETIMEDOUT|ECONNRESET|deadlock|exception)",
        stripped,
        re.I,
    ):
        return "error", stripped

    return None


def _iter_log_paths(run_dir: Path) -> Iterable[tuple[str, Path]]:
    logs_dir = resolve_kubernetes_logs_dir(run_dir)
    for name, source in _LOG_SOURCES:
        path = logs_dir / name
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

    errors_by_key: dict[str, _Acc] = defaultdict(_Acc)
    warnings_by_key: dict[str, _Acc] = defaultdict(_Acc)
    labels_by_class: dict[str, str] = {}
    source_stats: dict[str, LogSourceStats] = {}
    sources_scanned: list[str] = []
    lines_scanned = 0

    for source, path in _iter_log_paths(run_dir):
        sources_scanned.append(path.name)
        st = LogSourceStats(source=source)
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for raw in f:
                    lines_scanned += 1
                    st.lines_total += 1
                    parsed = parse_pod_log_line(raw)
                    if parsed is None:
                        continue
                    pod, _container, epoch, msg = parsed
                    classified = _classify_line(msg, source=source)
                    if classified is None:
                        continue
                    severity, body = classified
                    class_id, label = _classify_message(body)
                    labels_by_class[class_id] = _CLASS_LABELS.get(class_id, label)

                    if severity == "error":
                        st.errors += 1
                        bucket = errors_by_key
                        if load_start is not None and epoch < load_start:
                            st.pre_load_errors += 1
                        else:
                            st.under_load_errors += 1
                    else:
                        st.warnings += 1
                        bucket = warnings_by_key

                    acc = bucket[class_id]
                    acc.count += 1
                    acc.source_counts[source] = acc.source_counts.get(source, 0) + 1
                    acc.pods.add(pod)
                    if severity == "error":
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
        source_stats[source] = st

    def _build_rows(
        by_key: dict[str, _Acc], *, severity: str
    ) -> list[LogClassRow]:
        rows: list[LogClassRow] = []
        for class_id, acc in sorted(
            by_key.items(), key=lambda kv: kv[1].count, reverse=True
        )[:max_classes]:
            source_counts = tuple(
                sorted(acc.source_counts.items(), key=lambda kv: kv[1], reverse=True)
            )
            source = "+".join(src for src, _ in source_counts)
            rows.append(
                LogClassRow(
                    class_id=class_id,
                    label=labels_by_class.get(class_id, class_id),
                    severity=severity,
                    source=source,
                    count=acc.count,
                    pod_count=len(acc.pods),
                    pre_load_count=acc.pre_load,
                    under_load_count=acc.under_load,
                    first_seen_epoch=acc.first_epoch,
                    last_seen_epoch=acc.last_epoch,
                    example_line=acc.example,
                    source_counts=source_counts,
                    load_start_epoch=load_start,
                )
            )
        return rows

    return PodErrorSummary(
        sources=tuple(source_stats[src] for _name, src in _LOG_SOURCES if src in source_stats),
        error_rows=tuple(_build_rows(errors_by_key, severity="error")),
        warning_rows=tuple(_build_rows(warnings_by_key, severity="warning")),
        load_start_epoch=load_start,
        sources_scanned=tuple(sources_scanned),
        lines_scanned=lines_scanned,
    )

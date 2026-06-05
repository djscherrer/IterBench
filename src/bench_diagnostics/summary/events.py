"""Summarize notable Kubernetes events from ``events.jsonl``."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..paths import kubernetes_cluster_dir

_NOISE_REASONS = frozenset({"Pulled", "Created", "Started", "ScalingReplicaSet"})


@dataclass(frozen=True)
class EventRow:
    reason: str
    count: int
    message: str
    first_seen: str
    last_seen: str


@dataclass
class EventSummary:
    rows: tuple[EventRow, ...] = ()

    def to_prompt_block(self) -> str:
        if not self.rows:
            return "(no notable Warning/Error cluster events)"
        lines = [
            "Top cluster events (Warning/Error or failure-related):",
            "",
            "| Reason | Count | First seen | Last seen | Example |",
            "|---|---:|---|---|---|",
        ]
        for row in self.rows:
            msg = row.message.replace("|", "\\|")
            if len(msg) > 90:
                msg = msg[:87] + "…"
            lines.append(
                f"| {row.reason} | {row.count} | {row.first_seen or '?'} | "
                f"{row.last_seen or '?'} | {msg} |"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "rows": [
                {
                    "reason": r.reason,
                    "count": r.count,
                    "message": r.message,
                    "first_seen": r.first_seen,
                    "last_seen": r.last_seen,
                }
                for r in self.rows
            ]
        }

    @classmethod
    def from_dict(cls, data: dict) -> EventSummary:
        rows = tuple(
            EventRow(
                reason=str(r["reason"]),
                count=int(r["count"]),
                message=str(r["message"]),
                first_seen=str(r.get("first_seen", "")),
                last_seen=str(r.get("last_seen", "")),
            )
            for r in data.get("rows") or []
        )
        return cls(rows=rows)


def _is_notable(event: dict) -> bool:
    typ = (event.get("type") or "").strip()
    if typ in {"Warning", "Error"}:
        return True
    reason = (event.get("reason") or "").strip()
    if reason in _NOISE_REASONS:
        return False
    msg = (event.get("message") or "").lower()
    return any(k in msg for k in ("fail", "error", "backoff", "unhealthy", "kill"))


def summarize_cluster_events(run_dir: Path, *, max_rows: int = 8) -> EventSummary:
    path = kubernetes_cluster_dir(run_dir) / "events.jsonl"
    if not path.is_file():
        return EventSummary()

    counter: Counter[str] = Counter()
    examples: dict[str, str] = {}
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _is_notable(event):
                    continue
                reason = (event.get("reason") or "Unknown").strip()
                message = (event.get("message") or "").strip()
                key = f"{reason}|{message[:80]}"
                counter[key] += int(event.get("count") or 1)
                examples.setdefault(key, message)
                fs = str(event.get("first_seen") or "").strip()
                ls = str(event.get("last_seen") or event.get("first_seen") or "").strip()
                if fs and (key not in first_seen or fs < first_seen[key]):
                    first_seen[key] = fs
                if ls and (key not in last_seen or ls > last_seen[key]):
                    last_seen[key] = ls
    except OSError:
        return EventSummary()

    rows: list[EventRow] = []
    for key, count in counter.most_common(max_rows):
        reason = key.split("|", 1)[0]
        rows.append(
            EventRow(
                reason=reason,
                count=count,
                message=examples.get(key, ""),
                first_seen=_fmt_ts(first_seen.get(key, "")),
                last_seen=_fmt_ts(last_seen.get(key, "")),
            )
        )

    return EventSummary(rows=tuple(rows))


def _fmt_ts(iso: str) -> str:
    """``2026-06-03T08:44:46Z`` -> ``08:44:46`` (drop date for brevity)."""
    if not iso:
        return ""
    if "T" in iso:
        tail = iso.split("T", 1)[1]
        return tail.rstrip("Z")
    return iso

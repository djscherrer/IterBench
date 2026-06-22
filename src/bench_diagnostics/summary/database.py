"""Summarize PostgreSQL ``pg_stat_*`` CSV samples."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_kubernetes_metrics_database_dir
from ._stats import distribution_float, distribution_int
@dataclass(frozen=True)
class ActivityStateStats:
    state: str
    count_distribution: str
    max_age_s_distribution: str


@dataclass
class DatabaseSummary:
    activity_states: tuple[ActivityStateStats, ...] = ()
    peak_numbackends: int | None = None
    total_conn_distribution: str = ""
    deadlocks_delta: int | None = None
    xact_rollback_delta: int | None = None
    max_connections: int | None = None
    samples: int = 0

    def to_prompt_block(self) -> str:
        if self.samples == 0:
            return "(no PostgreSQL pg_stat samples found)"

        parts: list[str] = []

        # Headline: how close did we get to the connection ceiling?
        if self.peak_numbackends is not None:
            conn_line = (
                f"- **Peak concurrent connections**: {self.peak_numbackends}"
            )
            if self.max_connections:
                pct = 100.0 * self.peak_numbackends / self.max_connections
                conn_line += f" of {self.max_connections} allowed ({pct:.0f}%)"
            parts.append(conn_line)

        if self.total_conn_distribution:
            parts.append(
                f"- **Open connections (min/p50/avg/p95/max over samples)**: "
                f"{self.total_conn_distribution}"
            )

        if self.deadlocks_delta is not None:
            parts.append(f"- **Deadlocks during run**: {self.deadlocks_delta}")
        if self.xact_rollback_delta is not None:
            parts.append(
                f"- **Rolled-back transactions during run**: {self.xact_rollback_delta}"
            )

        if self.activity_states:
            parts.append("")
            parts.append(
                "Breakdown by connection state (count and oldest-session age, "
                "min/p50/avg/p95/max across samples):"
            )
            parts.append("")
            parts.append("| State | Count | Oldest session s |")
            parts.append("|---|---:|---:|")
            for st in self.activity_states:
                parts.append(
                    f"| {st.state} | {st.count_distribution} | "
                    f"{st.max_age_s_distribution} |"
                )

        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "peak_numbackends": self.peak_numbackends,
            "total_conn_distribution": self.total_conn_distribution,
            "deadlocks_delta": self.deadlocks_delta,
            "xact_rollback_delta": self.xact_rollback_delta,
            "max_connections": self.max_connections,
            "activity_states": [
                {
                    "state": s.state,
                    "count_distribution": s.count_distribution,
                    "max_age_s_distribution": s.max_age_s_distribution,
                }
                for s in self.activity_states
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> DatabaseSummary:
        states = tuple(
            ActivityStateStats(
                state=str(s["state"]),
                count_distribution=str(s.get("count_distribution") or ""),
                max_age_s_distribution=str(s.get("max_age_s_distribution") or ""),
            )
            for s in data.get("activity_states") or []
        )
        return cls(
            activity_states=states,
            peak_numbackends=data.get("peak_numbackends"),
            total_conn_distribution=str(data.get("total_conn_distribution") or ""),
            deadlocks_delta=data.get("deadlocks_delta"),
            xact_rollback_delta=data.get("xact_rollback_delta"),
            max_connections=data.get("max_connections"),
            samples=int(data.get("samples") or 0),
        )


def summarize_database_metrics(
    run_dir: Path,
    *,
    max_connections: int | None = None,
) -> DatabaseSummary:
    db_dir = resolve_kubernetes_metrics_database_dir(run_dir)
    activity_path = db_dir / "pg_stat_activity.csv"
    database_path = db_dir / "pg_stat_database.csv"

    activity_states: list[ActivityStateStats] = []
    samples = 0

    total_conn_distribution = ""

    if activity_path.is_file():
        with activity_path.open(newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
        samples = len({r.get("ts_epoch_s", "") for r in rows})
        counts_by_state: dict[str, list[int]] = defaultdict(list)
        ages_by_state: dict[str, list[float]] = defaultdict(list)
        total_by_ts: dict[str, int] = defaultdict(int)

        for row in rows:
            state = (row.get("state") or "").strip()
            if not state:
                continue
            try:
                count = int(float(row.get("count") or 0))
                max_age = float(row.get("max_age_s") or 0)
            except (TypeError, ValueError):
                continue
            counts_by_state[state].append(count)
            total_by_ts[row.get("ts_epoch_s", "")] += count
            if count > 0:
                ages_by_state[state].append(max_age)

        if total_by_ts:
            totals = list(total_by_ts.values())
            total_conn_distribution = distribution_int(totals)

        for state in sorted(counts_by_state.keys()):
            counts = counts_by_state[state]
            ages = ages_by_state.get(state) or [0.0]
            activity_states.append(
                ActivityStateStats(
                    state=state,
                    count_distribution=distribution_int(counts),
                    max_age_s_distribution=distribution_float(ages),
                )
            )

    peak_backends: int | None = None
    deadlocks_delta: int | None = None
    rollback_delta: int | None = None

    if database_path.is_file():
        with database_path.open(newline="", encoding="utf-8", errors="replace") as f:
            db_rows = list(csv.DictReader(f))
        if db_rows:
            def _int(row: dict, key: str) -> int:
                try:
                    return int(float(row.get(key) or 0))
                except (TypeError, ValueError):
                    return 0

            backends = [_int(r, "numbackends") for r in db_rows]
            peak_backends = max(backends) if backends else None
            first, last = db_rows[0], db_rows[-1]
            deadlocks_delta = _int(last, "deadlocks") - _int(first, "deadlocks")
            rollback_delta = _int(last, "xact_rollback") - _int(first, "xact_rollback")
            if samples == 0:
                samples = len(db_rows)

    return DatabaseSummary(
        activity_states=tuple(activity_states),
        peak_numbackends=peak_backends,
        total_conn_distribution=total_conn_distribution,
        deadlocks_delta=deadlocks_delta,
        xact_rollback_delta=rollback_delta,
        max_connections=max_connections,
        samples=samples,
    )

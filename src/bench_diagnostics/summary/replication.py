"""Summarize ``pg_stat_replication`` CSV samples."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_kubernetes_metrics_database_dir
from ._stats import DISTRIBUTION_LEGEND, distribution_float


@dataclass
class ReplicationMetricStats:
    metric: str
    min_p50_avg_p95_max: str


@dataclass
class ReplicationSummary:
    samples: int = 0
    replica_count: int = 0
    not_streaming: int = 0
    metrics: tuple[ReplicationMetricStats, ...] = ()

    def to_prompt_block(self) -> str:
        if self.samples == 0:
            return "(no replication samples — single-node Postgres or replicas not yet streaming)"
        parts = [
            "Replication lag from primary ``pg_stat_replication`` "
            "(seconds behind primary; **0** means replicas are caught up).",
            DISTRIBUTION_LEGEND,
            f"- **Replication samples**: {self.samples}",
            f"- **Replica streams per sample**: {self.replica_count}",
        ]
        for m in self.metrics:
            parts.append(f"- **{m.metric}** (min/p50/avg/p95/max s): {m.min_p50_avg_p95_max}")
        if self.not_streaming > 0:
            parts.append(
                f"- **Non-streaming replica observations**: {self.not_streaming}"
            )
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "replica_count": self.replica_count,
            "not_streaming": self.not_streaming,
            "metrics": [
                {"metric": m.metric, "min_p50_avg_p95_max": m.min_p50_avg_p95_max}
                for m in self.metrics
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReplicationSummary:
        return cls(
            samples=int(data.get("samples") or 0),
            replica_count=int(data.get("replica_count") or 0),
            not_streaming=int(data.get("not_streaming") or 0),
            metrics=tuple(
                ReplicationMetricStats(
                    metric=str(m["metric"]),
                    min_p50_avg_p95_max=str(m["min_p50_avg_p95_max"]),
                )
                for m in data.get("metrics") or []
            ),
        )


def summarize_replication_metrics(run_dir: Path) -> ReplicationSummary:
    path = resolve_kubernetes_metrics_database_dir(run_dir) / "pg_stat_replication.csv"
    if not path.is_file():
        return ReplicationSummary()

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ReplicationSummary()

    replay_lags: list[float] = []
    flush_lags: list[float] = []
    rows_per_ts: dict[str, int] = defaultdict(int)
    not_streaming = 0

    for row in rows:
        ts = (row.get("ts_epoch_s") or "").strip()
        if ts:
            rows_per_ts[ts] += 1
        state = (row.get("state") or "").strip().lower()
        if state and state != "streaming":
            not_streaming += 1
        try:
            replay_lags.append(float(row.get("replay_lag_s") or 0))
            flush_lags.append(float(row.get("flush_lag_s") or 0))
        except (TypeError, ValueError):
            continue

    metrics: list[ReplicationMetricStats] = []
    if replay_lags:
        metrics.append(
            ReplicationMetricStats(
                metric="replay_lag_s",
                min_p50_avg_p95_max=distribution_float(replay_lags),
            )
        )
    if flush_lags:
        metrics.append(
            ReplicationMetricStats(
                metric="flush_lag_s",
                min_p50_avg_p95_max=distribution_float(flush_lags),
            )
        )

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    replica_count = max(rows_per_ts.values()) if rows_per_ts else 0
    return ReplicationSummary(
        samples=samples,
        replica_count=replica_count,
        not_streaming=not_streaming,
        metrics=tuple(metrics),
    )

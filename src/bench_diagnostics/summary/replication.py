"""Summarize ``pg_stat_replication`` CSV samples."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..paths import kubernetes_database_dir


@dataclass
class ReplicationSummary:
    samples: int = 0
    max_replay_lag_s: float | None = None
    avg_replay_lag_s: float | None = None
    max_flush_lag_s: float | None = None
    replica_count: int = 0
    not_streaming: int = 0

    def to_prompt_block(self) -> str:
        if self.samples == 0:
            return "(no replication samples — single-node Postgres or replicas not yet streaming)"
        parts = [
            f"- **Replication samples**: {self.samples}",
            f"- **Replica streams observed**: {self.replica_count}",
        ]
        if self.max_replay_lag_s is not None:
            parts.append(
                f"- **Replay lag (avg/max)**: {self.avg_replay_lag_s:.2f}s / "
                f"{self.max_replay_lag_s:.2f}s"
            )
            if self.max_replay_lag_s > 5.0:
                parts.append(
                    "  — **high replay lag**; read replicas may serve stale data "
                    "or fall behind under write load"
                )
        if self.max_flush_lag_s is not None:
            parts.append(f"- **Flush lag (max)**: {self.max_flush_lag_s:.2f}s")
        if self.not_streaming > 0:
            parts.append(
                f"- **Non-streaming replica observations**: {self.not_streaming}"
            )
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "max_replay_lag_s": self.max_replay_lag_s,
            "avg_replay_lag_s": self.avg_replay_lag_s,
            "max_flush_lag_s": self.max_flush_lag_s,
            "replica_count": self.replica_count,
            "not_streaming": self.not_streaming,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReplicationSummary:
        return cls(
            samples=int(data.get("samples") or 0),
            max_replay_lag_s=data.get("max_replay_lag_s"),
            avg_replay_lag_s=data.get("avg_replay_lag_s"),
            max_flush_lag_s=data.get("max_flush_lag_s"),
            replica_count=int(data.get("replica_count") or 0),
            not_streaming=int(data.get("not_streaming") or 0),
        )


def summarize_replication_metrics(run_dir: Path) -> ReplicationSummary:
    path = kubernetes_database_dir(run_dir) / "pg_stat_replication.csv"
    if not path.is_file():
        return ReplicationSummary()

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ReplicationSummary()

    replay_lags: list[float] = []
    flush_lags: list[float] = []
    apps: set[str] = set()
    not_streaming = 0

    for row in rows:
        app = (row.get("application_name") or "").strip()
        state = (row.get("state") or "").strip().lower()
        if app:
            apps.add(app)
        if state and state != "streaming":
            not_streaming += 1
        try:
            replay_lags.append(float(row.get("replay_lag_s") or 0))
            flush_lags.append(float(row.get("flush_lag_s") or 0))
        except (TypeError, ValueError):
            continue

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    return ReplicationSummary(
        samples=samples,
        max_replay_lag_s=max(replay_lags) if replay_lags else None,
        avg_replay_lag_s=sum(replay_lags) / len(replay_lags) if replay_lags else None,
        max_flush_lag_s=max(flush_lags) if flush_lags else None,
        replica_count=len(apps),
        not_streaming=not_streaming,
    )

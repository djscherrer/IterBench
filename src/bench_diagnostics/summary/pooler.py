"""Summarize PgBouncer ``SHOW POOLS`` CSV samples."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_kubernetes_metrics_pooler_dir
from ._stats import DISTRIBUTION_LEGEND, distribution_int


@dataclass
class PoolerMetricStats:
    metric: str
    min_p50_avg_p95_max: str


@dataclass
class PoolerRoleStats:
    role: str
    metrics: tuple[PoolerMetricStats, ...] = ()


@dataclass
class PoolerSummary:
    roles: tuple[PoolerRoleStats, ...] = ()
    samples: int = 0

    def to_prompt_block(self) -> str:
        if not self.roles:
            return "(no PgBouncer pool samples — pooler disabled or not reachable)"
        parts = [
            "PgBouncer ``SHOW POOLS`` samples (``cl_active`` / ``cl_waiting`` = "
            "client connections active or queued).",
            DISTRIBUTION_LEGEND,
            f"- **Pool samples**: {self.samples}",
        ]
        for st in self.roles:
            parts.append(f"- **{st.role}**:")
            for m in st.metrics:
                parts.append(
                    f"  - {m.metric} (min/p50/avg/p95/max): {m.min_p50_avg_p95_max}"
                )
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "roles": [
                {
                    "role": r.role,
                    "metrics": [
                        {"metric": m.metric, "min_p50_avg_p95_max": m.min_p50_avg_p95_max}
                        for m in r.metrics
                    ],
                }
                for r in self.roles
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PoolerSummary:
        roles = tuple(
            PoolerRoleStats(
                role=str(r["role"]),
                metrics=tuple(
                    PoolerMetricStats(
                        metric=str(m["metric"]),
                        min_p50_avg_p95_max=str(m["min_p50_avg_p95_max"]),
                    )
                    for m in r.get("metrics") or []
                ),
            )
            for r in data.get("roles") or []
        )
        return cls(roles=roles, samples=int(data.get("samples") or 0))


_POOLER_METRICS = ("cl_active", "cl_waiting", "sv_active", "sv_idle")


def summarize_pooler_metrics(run_dir: Path) -> PoolerSummary:
    path = resolve_kubernetes_metrics_pooler_dir(run_dir) / "pgbouncer_pools.csv"
    if not path.is_file():
        return PoolerSummary()

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return PoolerSummary()

    by_role: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        role = (row.get("pooler_role") or "").strip() or "pooler"
        bucket = by_role.setdefault(role, {k: [] for k in _POOLER_METRICS})
        for key in _POOLER_METRICS:
            try:
                bucket[key].append(int(float(row.get(key) or 0)))
            except (TypeError, ValueError):
                pass

    roles: list[PoolerRoleStats] = []
    for role in sorted(by_role.keys()):
        b = by_role[role]
        metrics = tuple(
            PoolerMetricStats(
                metric=key,
                min_p50_avg_p95_max=distribution_int(b[key]),
            )
            for key in _POOLER_METRICS
            if b[key]
        )
        roles.append(PoolerRoleStats(role=role, metrics=metrics))

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    return PoolerSummary(roles=tuple(roles), samples=samples)

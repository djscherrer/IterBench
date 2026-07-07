"""Summarize Redis ``INFO`` CSV samples."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_kubernetes_metrics_cache_dir
from ._stats import DISTRIBUTION_LEGEND, distribution_int


def _int_or_zero(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


@dataclass
class CacheMetricStats:
    metric: str
    min_p50_avg_p95_max: str


@dataclass
class CacheSummary:
    samples: int = 0
    metrics: tuple[CacheMetricStats, ...] = ()
    keyspace_hits_start: int = 0
    keyspace_hits_end: int = 0
    keyspace_misses_start: int = 0
    keyspace_misses_end: int = 0

    def to_prompt_block(self) -> str:
        if self.samples <= 0:
            return ""
        parts = [
            "Redis ``INFO`` samples (memory, clients, command throughput).",
            DISTRIBUTION_LEGEND,
            f"- **Redis samples**: {self.samples}",
        ]
        for m in self.metrics:
            parts.append(f"- **{m.metric}** (min/p50/avg/p95/max): {m.min_p50_avg_p95_max}")
        hits_delta = self.keyspace_hits_end - self.keyspace_hits_start
        misses_delta = self.keyspace_misses_end - self.keyspace_misses_start
        total_delta = hits_delta + misses_delta
        hit_rate = (100.0 * hits_delta / total_delta) if total_delta > 0 else 0.0
        parts.append(
            f"- **keyspace_hits/misses during run**: "
            f"+{hits_delta:,} / +{misses_delta:,} "
            f"({hit_rate:.1f}% hit rate over run)"
        )
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "metrics": [
                {"metric": m.metric, "min_p50_avg_p95_max": m.min_p50_avg_p95_max}
                for m in self.metrics
            ],
            "keyspace_hits_start": self.keyspace_hits_start,
            "keyspace_hits_end": self.keyspace_hits_end,
            "keyspace_misses_start": self.keyspace_misses_start,
            "keyspace_misses_end": self.keyspace_misses_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CacheSummary:
        return cls(
            samples=int(data.get("samples") or 0),
            metrics=tuple(
                CacheMetricStats(
                    metric=str(m["metric"]),
                    min_p50_avg_p95_max=str(m["min_p50_avg_p95_max"]),
                )
                for m in data.get("metrics") or []
            ),
            keyspace_hits_start=int(data.get("keyspace_hits_start") or 0),
            keyspace_hits_end=int(data.get("keyspace_hits_end") or 0),
            keyspace_misses_start=int(data.get("keyspace_misses_start") or 0),
            keyspace_misses_end=int(data.get("keyspace_misses_end") or 0),
        )


_CACHE_SERIES = (
    "used_memory_bytes",
    "connected_clients",
    "evicted_keys",
    "instantaneous_ops_per_sec",
    "db_keys",
)


def summarize_cache_metrics(run_dir: Path) -> CacheSummary:
    path = resolve_kubernetes_metrics_cache_dir(run_dir) / "redis_info.csv"
    if not path.is_file():
        return CacheSummary()

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return CacheSummary()

    series: dict[str, list[int]] = {k: [] for k in _CACHE_SERIES}
    for row in rows:
        for key in _CACHE_SERIES:
            series[key].append(_int_or_zero(row.get(key, "")))

    metrics = tuple(
        CacheMetricStats(metric=key, min_p50_avg_p95_max=distribution_int(series[key]))
        for key in _CACHE_SERIES
        if series[key]
    )

    first, last = rows[0], rows[-1]
    return CacheSummary(
        samples=len(rows),
        metrics=metrics,
        keyspace_hits_start=_int_or_zero(first.get("keyspace_hits", "")),
        keyspace_hits_end=_int_or_zero(last.get("keyspace_hits", "")),
        keyspace_misses_start=_int_or_zero(first.get("keyspace_misses", "")),
        keyspace_misses_end=_int_or_zero(last.get("keyspace_misses", "")),
    )

"""Condense on-disk diagnostics into LLM-friendly summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .cache import CacheSummary, summarize_cache_metrics
from .database import DatabaseSummary, summarize_database_metrics
from .events import EventSummary, summarize_cluster_events
from .load_run import load_profile_from_config, summarize_load_run
from .pod_errors import PodErrorSummary, summarize_pod_errors
from .pod_health import PodHealthSummary, summarize_pod_health
from .pooler import PoolerSummary, summarize_pooler_metrics
from .replication import ReplicationSummary, summarize_replication_metrics
from .utilization import summarize_k8s_utilization


@dataclass
class DiagnosticsSummary:
    """Aggregated diagnostics for one bench run directory."""

    pod_errors: PodErrorSummary = field(default_factory=PodErrorSummary)
    database: DatabaseSummary = field(default_factory=DatabaseSummary)
    replication: ReplicationSummary = field(default_factory=ReplicationSummary)
    pooler: PoolerSummary = field(default_factory=PoolerSummary)
    cache: CacheSummary = field(default_factory=CacheSummary)
    pod_health: PodHealthSummary = field(default_factory=PodHealthSummary)
    events: EventSummary = field(default_factory=EventSummary)
    utilization: str = ""

    def to_prompt_block(self) -> str:
        sections = [
            ("### Pod logs", self.pod_errors.to_prompt_block()),
            ("### PostgreSQL", self.database.to_prompt_block()),
            ("### Replication lag", self.replication.to_prompt_block()),
            ("### PgBouncer pools", self.pooler.to_prompt_block()),
            ("### Redis cache", self.cache.to_prompt_block()),
            ("### Pod health", self.pod_health.to_prompt_block()),
            ("### Cluster events", self.events.to_prompt_block()),
            ("### Kubernetes utilization", self.utilization or "(kubernetes metrics unavailable)"),
        ]
        parts: list[str] = []
        for heading, body in sections:
            parts.append(heading)
            parts.append("")
            parts.append(body)
            parts.append("")
        return "\n".join(parts).rstrip()

    def to_dict(self) -> dict:
        return {
            "pod_errors": self.pod_errors.to_dict(),
            "database": self.database.to_dict(),
            "replication": self.replication.to_dict(),
            "pooler": self.pooler.to_dict(),
            "cache": self.cache.to_dict(),
            "pod_health": self.pod_health.to_dict(),
            "events": self.events.to_dict(),
            "utilization": self.utilization,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiagnosticsSummary:
        return cls(
            pod_errors=PodErrorSummary.from_dict(data.get("pod_errors") or {}),
            database=DatabaseSummary.from_dict(data.get("database") or {}),
            replication=ReplicationSummary.from_dict(data.get("replication") or {}),
            pooler=PoolerSummary.from_dict(data.get("pooler") or {}),
            cache=CacheSummary.from_dict(data.get("cache") or {}),
            pod_health=PodHealthSummary.from_dict(data.get("pod_health") or {}),
            events=EventSummary.from_dict(data.get("events") or {}),
            utilization=str(data.get("utilization") or ""),
        )


def summarize_run_dir(
    run_dir: Path,
    *,
    bench_log: str = "",
    max_connections: int | None = None,
) -> DiagnosticsSummary:
    """
    Build a :class:`DiagnosticsSummary` from ``<run_dir>/diagnostics/kubernetes/``.

    ``bench_log`` is used for the pre-load / under-load error split and is
    optional but recommended.
    """
    return DiagnosticsSummary(
        pod_errors=summarize_pod_errors(run_dir, bench_log=bench_log),
        database=summarize_database_metrics(run_dir, max_connections=max_connections),
        replication=summarize_replication_metrics(run_dir),
        pooler=summarize_pooler_metrics(run_dir),
        cache=summarize_cache_metrics(run_dir),
        pod_health=summarize_pod_health(run_dir),
        events=summarize_cluster_events(run_dir),
        utilization=summarize_k8s_utilization(run_dir),
    )


def benchmark_context_from_config(config: dict) -> str:
    """Short context block (scenario identity only; sizing is in conversation history)."""
    spec = config.get("k8s_workload_spec") or {}
    labels = spec.get("labels") or {}
    scenario = labels.get("baxbench.dev/scenario", "")
    env = labels.get("baxbench.dev/env", "")
    iteration = (spec.get("metadata") or {}).get("iteration_id") or (
        (config.get("k8s_iteration") or {}).get("id")
    )

    lines = [
        f"- **Scenario**: {scenario or '(unknown)'}",
        f"- **Framework / environment**: {env or '(unknown)'}",
    ]
    if iteration:
        lines.append(f"- **Iteration**: {iteration}")
    return "\n".join(lines)


def read_run_config(run_dir: Path) -> dict:
    cfg_path = run_dir / "config.json"
    if not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


__all__ = [
    "DiagnosticsSummary",
    "benchmark_context_from_config",
    "load_profile_from_config",
    "read_run_config",
    "summarize_load_run",
    "summarize_run_dir",
]

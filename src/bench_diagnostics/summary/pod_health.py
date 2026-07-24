"""Summarize pod restart / readiness from ``pod_status.csv``."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_kubernetes_metrics_cluster_dir


@dataclass
class PodHealthSummary:
    pods_tracked: int = 0
    max_restart_count: int = 0
    pods_with_restarts: tuple[str, ...] = ()
    pods_not_ready: tuple[str, ...] = ()
    termination_reasons: tuple[str, ...] = ()

    def to_prompt_block(self) -> str:
        if self.pods_tracked == 0:
            return "(no pod status samples found)"
        lines = [
            f"Tracked {self.pods_tracked} pod(s); max restart count: {self.max_restart_count}.",
        ]
        if self.pods_with_restarts:
            names = ", ".join(f"`{p}`" for p in self.pods_with_restarts[:8])
            extra = f" (+{len(self.pods_with_restarts) - 8} more)" if len(self.pods_with_restarts) > 8 else ""
            lines.append(f"- **Pods with restarts**: {names}{extra}")
        if self.pods_not_ready:
            names = ", ".join(f"`{p}`" for p in self.pods_not_ready[:8])
            lines.append(f"- **Pods not ready at some point**: {names}")
        if self.termination_reasons:
            reasons = ", ".join(f"`{r}`" for r in self.termination_reasons)
            lines.append(f"- **Last termination reasons seen**: {reasons}")
        if (
            not self.pods_with_restarts
            and not self.pods_not_ready
            and not self.termination_reasons
        ):
            lines.append("- No restarts or readiness issues detected during the run.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "pods_tracked": self.pods_tracked,
            "max_restart_count": self.max_restart_count,
            "pods_with_restarts": list(self.pods_with_restarts),
            "pods_not_ready": list(self.pods_not_ready),
            "termination_reasons": list(self.termination_reasons),
        }

    @classmethod
    def from_dict(cls, data: dict) -> PodHealthSummary:
        return cls(
            pods_tracked=int(data.get("pods_tracked") or 0),
            max_restart_count=int(data.get("max_restart_count") or 0),
            pods_with_restarts=tuple(data.get("pods_with_restarts") or ()),
            pods_not_ready=tuple(data.get("pods_not_ready") or ()),
            termination_reasons=tuple(data.get("termination_reasons") or ()),
        )


def summarize_pod_health(run_dir: Path) -> PodHealthSummary:
    path = resolve_kubernetes_metrics_cluster_dir(run_dir) / "pod_status.csv"
    if not path.is_file():
        return PodHealthSummary()

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))

    restart_by_pod: dict[str, int] = {}
    not_ready: set[str] = set()
    term_reasons: set[str] = set()

    for row in rows:
        pod = (row.get("pod") or "").strip()
        if not pod:
            continue
        try:
            restarts = int(float(row.get("restart_count") or 0))
        except (TypeError, ValueError):
            restarts = 0
        restart_by_pod[pod] = max(restart_by_pod.get(pod, 0), restarts)

        ready = (row.get("ready") or "").strip()
        if ready not in {"1", "true", "True"}:
            not_ready.add(pod)

        reason = (row.get("last_termination_reason") or "").strip()
        if reason:
            term_reasons.add(reason)

    pods_with_restarts = tuple(
        sorted(p for p, c in restart_by_pod.items() if c > 0)
    )
    max_restart = max(restart_by_pod.values()) if restart_by_pod else 0

    return PodHealthSummary(
        pods_tracked=len(restart_by_pod),
        max_restart_count=max_restart,
        pods_with_restarts=pods_with_restarts,
        pods_not_ready=tuple(sorted(not_ready)),
        termination_reasons=tuple(sorted(term_reasons)),
    )

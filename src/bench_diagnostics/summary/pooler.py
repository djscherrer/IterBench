"""Summarize PgBouncer ``SHOW POOLS`` CSV samples."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..paths import kubernetes_pooler_dir


@dataclass
class PoolerRoleStats:
    role: str
    peak_cl_active: int
    peak_cl_waiting: int
    peak_sv_active: int
    peak_sv_idle: int


@dataclass
class PoolerSummary:
    roles: tuple[PoolerRoleStats, ...] = ()
    samples: int = 0

    def to_prompt_block(self) -> str:
        if not self.roles:
            return "(no PgBouncer pool samples — pooler disabled or not reachable)"
        parts = [f"- **Pool samples**: {self.samples}"]
        for st in self.roles:
            parts.append(
                f"- **{st.role}**: peak cl_active={st.peak_cl_active}, "
                f"cl_waiting={st.peak_cl_waiting}, sv_active={st.peak_sv_active}, "
                f"sv_idle={st.peak_sv_idle}"
            )
            if st.peak_cl_waiting > 0:
                parts.append(
                    f"  — **clients waiting** on {st.role}; raise pool size or "
                    "scale pooler replicas"
                )
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "roles": [
                {
                    "role": r.role,
                    "peak_cl_active": r.peak_cl_active,
                    "peak_cl_waiting": r.peak_cl_waiting,
                    "peak_sv_active": r.peak_sv_active,
                    "peak_sv_idle": r.peak_sv_idle,
                }
                for r in self.roles
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PoolerSummary:
        roles = tuple(
            PoolerRoleStats(
                role=str(r["role"]),
                peak_cl_active=int(r["peak_cl_active"]),
                peak_cl_waiting=int(r["peak_cl_waiting"]),
                peak_sv_active=int(r["peak_sv_active"]),
                peak_sv_idle=int(r["peak_sv_idle"]),
            )
            for r in data.get("roles") or []
        )
        return cls(roles=roles, samples=int(data.get("samples") or 0))


def summarize_pooler_metrics(run_dir: Path) -> PoolerSummary:
    path = kubernetes_pooler_dir(run_dir) / "pgbouncer_pools.csv"
    if not path.is_file():
        return PoolerSummary()

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return PoolerSummary()

    by_role: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        role = (row.get("pooler_role") or "").strip() or "pooler"
        bucket = by_role.setdefault(
            role,
            {"cl_active": [], "cl_waiting": [], "sv_active": [], "sv_idle": []},
        )
        for key in bucket:
            try:
                bucket[key].append(int(float(row.get(key) or 0)))
            except (TypeError, ValueError):
                pass

    roles: list[PoolerRoleStats] = []
    for role in sorted(by_role.keys()):
        b = by_role[role]
        roles.append(
            PoolerRoleStats(
                role=role,
                peak_cl_active=max(b["cl_active"]) if b["cl_active"] else 0,
                peak_cl_waiting=max(b["cl_waiting"]) if b["cl_waiting"] else 0,
                peak_sv_active=max(b["sv_active"]) if b["sv_active"] else 0,
                peak_sv_idle=max(b["sv_idle"]) if b["sv_idle"] else 0,
            )
        )

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    return PoolerSummary(roles=tuple(roles), samples=samples)

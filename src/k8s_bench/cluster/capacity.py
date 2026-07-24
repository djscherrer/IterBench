"""
Read Kubernetes node allocatable resources for agent deployment tuning.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .preflight import _kubectl


def _parse_cpu_to_millicores(value: str) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(float(text) * 1000)


def _parse_memory_to_bytes(value: str) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTPE]i?)?", text)
    if not m:
        return 0
    num = float(m.group(1))
    unit = (m.group(2) or "").upper()
    mult = {
        "": 1,
        "K": 10**3,
        "KI": 2**10,
        "M": 10**6,
        "MI": 2**20,
        "G": 10**9,
        "GI": 2**30,
        "T": 10**12,
        "TI": 2**40,
    }.get(unit, 1)
    return int(num * mult)


def _node_roles(labels: dict[str, str]) -> tuple[str, ...]:
    roles: list[str] = []
    if labels.get("node-role.kubernetes.io/control-plane") == "":
        roles.append("control-plane")
    if labels.get("node-role.kubernetes.io/master") == "":
        roles.append("control-plane")
    if labels.get("node-role.kubernetes.io/worker") == "":
        roles.append("worker")
    if not roles:
        roles.append("worker")
    return tuple(dict.fromkeys(roles))


def _node_schedulable(node: dict[str, Any]) -> bool:
    spec = node.get("spec") or {}
    if spec.get("unschedulable"):
        return False
    for cond in node.get("status", {}).get("conditions") or []:
        if cond.get("type") == "Ready" and cond.get("status") != "True":
            return False
    return True


@dataclass(frozen=True)
class NodeCapacity:
    name: str
    roles: tuple[str, ...]
    schedulable: bool
    allocatable_cpu_millicores: int
    allocatable_memory_bytes: int

    @property
    def is_control_plane(self) -> bool:
        return "control-plane" in self.roles

    @property
    def is_worker(self) -> bool:
        return self.schedulable and not self.is_control_plane

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "roles": list(self.roles),
            "schedulable": self.schedulable,
            "allocatable_cpu": f"{self.allocatable_cpu_millicores}m",
            "allocatable_memory_gi": round(
                self.allocatable_memory_bytes / (2**30), 2
            ),
        }


@dataclass(frozen=True)
class ClusterCapacity:
    nodes: tuple[NodeCapacity, ...]
    ready_nodes: int
    worker_nodes: tuple[NodeCapacity, ...]
    total_worker_cpu_millicores: int
    total_worker_memory_bytes: int
    suggested_reserve_fraction: float = 0.15

    @property
    def worker_count(self) -> int:
        return len(self.worker_nodes)

    @property
    def budget_cpu_millicores(self) -> int:
        return int(self.total_worker_cpu_millicores * (1 - self.suggested_reserve_fraction))

    @property
    def budget_memory_bytes(self) -> int:
        return int(self.total_worker_memory_bytes * (1 - self.suggested_reserve_fraction))

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "ready_nodes": self.ready_nodes,
            "worker_count": self.worker_count,
            "suggested_reserve_fraction": self.suggested_reserve_fraction,
            "workers": [n.to_dict() for n in self.worker_nodes],
            "total_worker_cpu": f"{self.total_worker_cpu_millicores}m",
            "total_worker_memory_gi": round(
                self.total_worker_memory_bytes / (2**30), 2
            ),
            "budget_cpu_after_reserve": f"{self.budget_cpu_millicores}m",
            "budget_memory_gi_after_reserve": round(
                self.budget_memory_bytes / (2**30), 2
            ),
        }


def collect_cluster_capacity(
    logger: logging.Logger | None = None,
    *,
    timeout_s: int = 60,
) -> ClusterCapacity:
    """Summarize allocatable CPU/memory on schedulable worker nodes."""
    log = logger or logging.getLogger(__name__)
    proc = _kubectl(["get", "nodes", "-o", "json"], timeout_s=timeout_s)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"kubectl get nodes failed: {err}")

    raw = json.loads(proc.stdout or "{}")
    items = raw.get("items") or []
    nodes: list[NodeCapacity] = []
    ready = 0

    for item in items:
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "")
        labels = {str(k): str(v) for k, v in (meta.get("labels") or {}).items()}
        alloc = (item.get("status") or {}).get("allocatable") or {}
        schedulable = _node_schedulable(item)
        if schedulable:
            ready += 1
        nodes.append(
            NodeCapacity(
                name=name,
                roles=_node_roles(labels),
                schedulable=schedulable,
                allocatable_cpu_millicores=_parse_cpu_to_millicores(
                    str(alloc.get("cpu", "0"))
                ),
                allocatable_memory_bytes=_parse_memory_to_bytes(
                    str(alloc.get("memory", "0"))
                ),
            )
        )

    workers = tuple(n for n in nodes if n.is_worker)
    if not workers:
        workers = tuple(n for n in nodes if n.schedulable)
        log.warning(
            "No non-control-plane workers found; using all schedulable nodes for capacity"
        )

    total_cpu = sum(n.allocatable_cpu_millicores for n in workers)
    total_mem = sum(n.allocatable_memory_bytes for n in workers)
    cap = ClusterCapacity(
        nodes=tuple(nodes),
        ready_nodes=ready,
        worker_nodes=workers,
        total_worker_cpu_millicores=total_cpu,
        total_worker_memory_bytes=total_mem,
    )
    budget_cores = cap.budget_cpu_millicores / 1000.0
    log.info(
        "Cluster capacity: %d worker(s), budget ~%.1f CPU cores (%dm) / ~%.1f Gi memory after reserve",
        cap.worker_count,
        budget_cores,
        cap.budget_cpu_millicores,
        cap.budget_memory_bytes / (2**30),
    )
    return cap


def capacity_as_json(capacity: ClusterCapacity) -> str:
    return json.dumps(capacity.to_prompt_dict(), indent=2, sort_keys=True)

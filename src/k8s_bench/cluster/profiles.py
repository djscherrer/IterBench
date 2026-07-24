"""
Named Kubernetes *cluster* profiles (where to deploy).

Workload layout (replicas, CPU, DB) still lives in per-sample
``iterations/iteration-NNN/spec/spec.yaml``. Cluster profiles select kubeconfig,
lab topology (control / workers / Locust), and optional image registry (``host:5000``).

Topology is defined only in ``K8S_CLUSTER_REGISTRY``. Select a profile via
``--k8s-cluster`` (or pass ``profile_name`` from bench code); there are no
host-list overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence


def _dedupe_hosts(hosts: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in hosts:
        h = (raw or "").strip()
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return tuple(out)


@dataclass(frozen=True)
class K8sClusterProfile:
    name: str
    description: str = ""
    kube_context: str | None = None
    kubeconfig_path: str | None = None
    # Kubernetes: single control-plane host + kubeadm worker nodes.
    control_node: str = ""
    worker_nodes: tuple[str, ...] = ()
    # Locust: one master host + optional worker hosts (may include the master).
    load_master: str = ""
    load_workers: tuple[str, ...] = ()
    notes: str = ""
    # Private registry on control-plane (HTTP). BaxBench pushes; nodes pull with IfNotPresent.
    registry_enabled: bool = False
    registry_host: str = ""  # empty + registry_auto_host → detect primary IP on bench host
    registry_port: int = 5000
    registry_auto_host: bool = True

    @property
    def k8s_ssh_hosts(self) -> tuple[str, ...]:
        """Control-plane + Kubernetes workers (preflight, registry setup, kubeadm)."""
        hosts: list[str] = []
        if self.control_node.strip():
            hosts.append(self.control_node.strip())
        hosts.extend(self.worker_nodes)
        return _dedupe_hosts(hosts)

    @property
    def locust_hosts(self) -> tuple[str, ...]:
        """All Locust SSH hosts (master + workers), deduped."""
        hosts: list[str] = []
        if self.load_master.strip():
            hosts.append(self.load_master.strip())
        hosts.extend(self.load_workers)
        return _dedupe_hosts(hosts)

    def has_k8s_topology(self) -> bool:
        return bool(self.control_node.strip())

    def has_load_topology(self) -> bool:
        return bool(self.load_master.strip())

    def to_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.kubeconfig_path:
            out["KUBECONFIG"] = os.path.expanduser(self.kubeconfig_path)
        if self.registry_enabled and self.registry_host:
            out["BAXBENCH_REGISTRY"] = f"{self.registry_host}:{self.registry_port}"
        return out


K8S_CLUSTER_REGISTRY: dict[str, K8sClusterProfile] = {
    "lab-default": K8sClusterProfile(
        name="lab-default",
        description="Shared lab Kubernetes (edit kubeconfig_path and topology).",
        kube_context="lab",
        kubeconfig_path="~/.kube/config-lab",
        notes="Workloads run on cluster nodes; use spec.yaml for replica/resource tuning.",
    ),
    "baxbench-emulab": K8sClusterProfile(
        name="baxbench-emulab",
        description=(
            "Emulab: node0 control+BaxBench+registry :5000; "
            "node1 Locust master + 30 workers, node2 32 workers; workers node3–5."
        ),
        kube_context="kubernetes-admin@kubernetes",
        kubeconfig_path="/tmp/dscherre/.kube/config-baxbench-emulab",
        control_node="node0",
        worker_nodes=("node5", "node6", "node7", "node8"),
        load_master="node1",
        load_workers=(
            "node1", "node1", "node1", "node1", "node1", "node1", "node1", "node1",
            "node1", "node1", "node1", "node1", "node1", "node1", "node1", "node1",

            "node2", "node2", "node2", "node2", "node2", "node2", "node2", "node2",
            "node2", "node2", "node2", "node2", "node2", "node2", "node2", "node2",
            "node2", "node2", "node2", "node2", "node2", "node2", "node2", "node2",
            "node2", "node2", "node2", "node2", "node2", "node2", "node2", "node2",

            "node3", "node3", "node3", "node3", "node3", "node3", "node3", "node3",
            "node3", "node3", "node3", "node3", "node3", "node3", "node3", "node3",
            "node3", "node3", "node3", "node3", "node3", "node3", "node3", "node3",
            "node3", "node3", "node3", "node3", "node3", "node3", "node3", "node3",

            "node4", "node4", "node4", "node4", "node4", "node4", "node4", "node4",
            "node4", "node4", "node4", "node4", "node4", "node4", "node4", "node4",
            "node4", "node4", "node4", "node4", "node4", "node4", "node4", "node4",
            "node4", "node4", "node4", "node4", "node4", "node4", "node4", "node4",

        ),
        registry_enabled=True,
        registry_host="",  # auto-detect node0 lab IP at bench time
        registry_port=5000,
        registry_auto_host=True,
        notes="Registry is configured by ./scripts/k8s_setup_cluster.sh when registry_enabled=true.",
    ),
}


def resolve_cluster_profile(name: str | None) -> K8sClusterProfile:
    key = (name or "").strip()
    if key not in K8S_CLUSTER_REGISTRY:
        known = ", ".join(sorted(K8S_CLUSTER_REGISTRY))
        raise ValueError(f"Unknown K8s cluster profile '{key}'. Known: {known}")
    return K8S_CLUSTER_REGISTRY[key]


def selected_cluster_profile_name(
    profile_name: str | None = None,
    *,
    args: Any | None = None,
) -> str:
    """Profile name from an explicit ``profile_name`` or ``--k8s-cluster`` on ``args``."""
    if profile_name and profile_name.strip():
        return profile_name.strip()
    if args is not None:
        from_cli = (getattr(args, "k8s_cluster", None) or "").strip()
        if from_cli:
            return from_cli
    raise ValueError(
        "Pass --k8s-cluster (or profile_name=…) to select a profile from "
        "k8s_bench/cluster/profiles.py (edit K8S_CLUSTER_REGISTRY to change topology)."
    )


def selected_cluster_profile(
    profile_name: str | None = None,
    *,
    args: Any | None = None,
) -> K8sClusterProfile:
    return resolve_cluster_profile(
        selected_cluster_profile_name(profile_name, args=args)
    )

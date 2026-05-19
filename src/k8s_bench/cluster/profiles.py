"""
Named Kubernetes *cluster* profiles (where to deploy), parallel to
``distributed_bench.system_configs`` (which SSH hosts run what).

Workload layout (replicas, CPU, DB) still lives in per-sample
``k8s_configs/<iteration>/spec.yaml``. Cluster profiles select kubeconfig
and optional lab image registry (``host:5000``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class K8sClusterProfile:
    name: str
    description: str = ""
    kube_context: str | None = None
    kubeconfig_path: str | None = None
    node_hosts: tuple[str, ...] = ()
    load_hosts: tuple[str, ...] = ()
    notes: str = ""
    # Private registry on control-plane (HTTP). BaxBench pushes; nodes pull with IfNotPresent.
    registry_enabled: bool = False
    registry_host: str = ""  # empty + registry_auto_host → detect primary IP on bench host
    registry_port: int = 5000
    registry_auto_host: bool = True

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
        description="Shared lab Kubernetes (edit kubeconfig_path and context).",
        kube_context="lab",
        kubeconfig_path="~/.kube/config-lab",
        node_hosts=(),
        notes="Workloads run on cluster nodes; use spec.yaml for replica/resource tuning.",
    ),
    "baxbench-emulab": K8sClusterProfile(
        name="baxbench-emulab",
        description="Emulab: CP+BaxBench on node0, registry :5000, workers node2–5.",
        kube_context="kubernetes-admin@kubernetes",
        kubeconfig_path="/tmp/dscherre/.kube/config-baxbench-emulab",
        node_hosts=("node0", "node2", "node3", "node4", "node5"),
        load_hosts=(),
        registry_enabled=True,
        registry_host="",  # auto-detect node0 lab IP at bench time
        registry_port=5000,
        registry_auto_host=True,
        notes="Run ./scripts/k8s_setup_registry.sh once after k8s_setup_cluster.sh.",
    ),
}


def resolve_cluster_profile(name: str | None) -> K8sClusterProfile:
    key = (name or "").strip()
    if key not in K8S_CLUSTER_REGISTRY:
        known = ", ".join(sorted(K8S_CLUSTER_REGISTRY))
        raise ValueError(f"Unknown K8s cluster profile '{key}'. Known: {known}")
    return K8S_CLUSTER_REGISTRY[key]

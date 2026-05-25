from __future__ import annotations

from typing import Any

from .models import K8sWorkloadSpec


def _required_node_affinity(node_names: tuple[str, ...]) -> dict[str, Any] | None:
    if not node_names:
        return None
    return {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": list(node_names),
                        }
                    ]
                }
            ]
        }
    }


def _backend_spread_anti_affinity() -> dict[str, Any]:
    return {
        "preferredDuringSchedulingIgnoredDuringExecution": [
            {
                "weight": 100,
                "podAffinityTerm": {
                    "labelSelector": {
                        "matchLabels": {"app": "backend"},
                    },
                    "topologyKey": "kubernetes.io/hostname",
                },
            }
        ]
    }


def _postgres_spread_anti_affinity() -> dict[str, Any]:
    return {
        "preferredDuringSchedulingIgnoredDuringExecution": [
            {
                "weight": 100,
                "podAffinityTerm": {
                    "labelSelector": {
                        "matchExpressions": [
                            {
                                "key": "baxbench.dev/db-tier",
                                "operator": "In",
                                "values": ["primary", "replica"],
                            }
                        ],
                    },
                    "topologyKey": "kubernetes.io/hostname",
                },
            }
        ]
    }


def _pod_spec_affinity(
    spec: K8sWorkloadSpec,
    *,
    role: str,
    node_names: tuple[str, ...],
    spread: bool = False,
) -> dict[str, Any]:
    pod_spec: dict[str, Any] = {}
    affinity: dict[str, Any] = {}
    node_aff = _required_node_affinity(node_names)
    if node_aff:
        affinity["nodeAffinity"] = node_aff
    if spread and role == "backend" and spec.backend.replicas > 1:
        affinity["podAntiAffinity"] = _backend_spread_anti_affinity()
    if spread and role == "db" and spec.database.replicas > 1:
        affinity["podAntiAffinity"] = _postgres_spread_anti_affinity()
    if affinity:
        pod_spec["affinity"] = affinity
    return pod_spec


def _postgres_container_args(spec: K8sWorkloadSpec) -> list[str]:
    if not spec.database.enabled:
        return []
    return [
        "-c",
        f"max_connections={spec.database.max_connections}",
    ]

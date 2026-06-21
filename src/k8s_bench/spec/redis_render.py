"""Render Redis Deployment + Service for optional cache tiers."""

from __future__ import annotations

from typing import Any

from .cache import DEFAULT_REDIS_IMAGE, CacheSpec
from .models import K8sWorkloadSpec
from .placement import _pod_spec_affinity


def _redis_manifests_for(
    spec: K8sWorkloadSpec,
    *,
    cache: CacheSpec,
    role: str,
    common_labels: dict[str, str],
) -> list[dict[str, Any]]:
    name = cache.service_name
    labels = {**common_labels, "baxbench.dev/role": role, "app": name}
    selector = {"app": name}
    pod_spec = _pod_spec_affinity(
        spec, role=role, node_names=(), spread=cache.replicas > 1
    )
    container: dict[str, Any] = {
        "name": "redis",
        "image": DEFAULT_REDIS_IMAGE,
        "ports": [{"containerPort": cache.port}],
        "command": ["redis-server"],
        "args": [
            "--maxmemory",
            cache.maxmemory,
            "--maxmemory-policy",
            cache.maxmemory_policy,
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        "resources": cache.resources.to_k8s_resources(),
        "readinessProbe": {
            "exec": {"command": ["redis-cli", "ping"]},
            "initialDelaySeconds": 3,
            "periodSeconds": 5,
            "failureThreshold": 6,
        },
    }
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": spec.namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": cache.replicas,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        **pod_spec,
                        "containers": [container],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": spec.namespace,
                "labels": labels,
            },
            "spec": {
                "selector": selector,
                "ports": [{"port": cache.port, "targetPort": cache.port}],
            },
        },
    ]


def build_redis_manifests(
    spec: K8sWorkloadSpec,
    *,
    common_labels: dict[str, str],
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    if spec.cache.enabled:
        docs.extend(
            _redis_manifests_for(
                spec, cache=spec.cache, role="cache", common_labels=common_labels
            )
        )
    db_cache = spec.database.cache
    if db_cache.enabled and not db_cache.use_shared:
        dedicated = CacheSpec(
            enabled=True,
            service_name=db_cache.service_name,
            port=db_cache.port,
            replicas=db_cache.replicas,
            maxmemory=db_cache.maxmemory,
            maxmemory_policy=db_cache.maxmemory_policy,
            resources=db_cache.resources,
        )
        docs.extend(
            _redis_manifests_for(
                spec,
                cache=dedicated,
                role="db-cache",
                common_labels=common_labels,
            )
        )
    return docs

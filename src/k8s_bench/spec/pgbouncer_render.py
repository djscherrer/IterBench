"""Render PgBouncer Deployment + Service (primary and read-replica poolers)."""

from __future__ import annotations

from typing import Any

from .models import K8sWorkloadSpec, POSTGRES_DATABASE, POSTGRES_PASSWORD, POSTGRES_USER
from .placement import _pod_spec_affinity
from .pooler import DEFAULT_READ_POOLER_SERVICE, PGBOUNCER_IMAGE, PoolerSpec


def _pgbouncer_env(
    *,
    pooler: PoolerSpec,
    pg_host: str,
    pg_port: int,
) -> list[dict[str, str]]:
    env: list[dict[str, str]] = [
        {"name": "DB_HOST", "value": pg_host},
        {"name": "DB_PORT", "value": str(pg_port)},
        {"name": "DB_USER", "value": POSTGRES_USER},
        {"name": "DB_PASSWORD", "value": POSTGRES_PASSWORD},
        {"name": "DB_NAME", "value": POSTGRES_DATABASE},
        {"name": "AUTH_TYPE", "value": "plain"},
        {"name": "POOL_MODE", "value": pooler.mode},
        {"name": "MAX_CLIENT_CONN", "value": str(pooler.max_client_conn)},
        {"name": "DEFAULT_POOL_SIZE", "value": str(pooler.default_pool_size)},
        {"name": "LISTEN_PORT", "value": str(pooler.port)},
    ]
    if pooler.min_pool_size is not None:
        env.append({"name": "MIN_POOL_SIZE", "value": str(pooler.min_pool_size)})
    if pooler.reserve_pool_size is not None:
        env.append(
            {"name": "RESERVE_POOL_SIZE", "value": str(pooler.reserve_pool_size)}
        )
    if pooler.mode == "transaction":
        env.append({"name": "SERVER_RESET_QUERY", "value": "DISCARD ALL"})
    return env


def _build_single_pooler(
    spec: K8sWorkloadSpec,
    *,
    pooler: PoolerSpec,
    pg_host: str,
    pg_port: int,
    role: str,
    common_labels: dict[str, str],
) -> list[dict[str, Any]]:
    name = pooler.service_name
    labels = {**common_labels, "baxbench.dev/role": role, "app": name}
    selector = {"app": name}
    pod_spec = _pod_spec_affinity(
        spec, role=role, node_names=(), spread=pooler.replicas > 1
    )
    container: dict[str, Any] = {
        "name": "pgbouncer",
        "image": PGBOUNCER_IMAGE,
        "ports": [{"containerPort": pooler.port}],
        "env": _pgbouncer_env(pooler=pooler, pg_host=pg_host, pg_port=pg_port),
        "resources": pooler.resources.to_k8s_resources(),
        "readinessProbe": {
            "tcpSocket": {"port": pooler.port},
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
                "replicas": pooler.replicas,
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
                "ports": [{"port": pooler.port, "targetPort": pooler.port}],
            },
        },
    ]


def build_pgbouncer_manifests(
    spec: K8sWorkloadSpec,
    *,
    common_labels: dict[str, str],
) -> list[dict[str, Any]]:
    if not spec.database.enabled:
        return []

    docs: list[dict[str, Any]] = []
    if spec.pooler.enabled:
        pg_host = f"{spec.database.service_name}.{spec.namespace}.svc.cluster.local"
        docs.extend(
            _build_single_pooler(
                spec,
                pooler=spec.pooler,
                pg_host=pg_host,
                pg_port=spec.database.port,
                role="pooler",
                common_labels=common_labels,
            )
        )
    if spec.read_pooler.enabled and spec.database.replicas > 1:
        read_host = (
            f"{spec.database.service_name}-read."
            f"{spec.namespace}.svc.cluster.local"
        )
        docs.extend(
            _build_single_pooler(
                spec,
                pooler=spec.read_pooler,
                pg_host=read_host,
                pg_port=spec.database.port,
                role="read-pooler",
                common_labels=common_labels,
            )
        )
    return docs


def default_read_pooler_spec() -> PoolerSpec:
    """Default read pooler: separate service name, same port as primary pooler."""
    from .pooler import PoolerSpec

    return PoolerSpec(
        enabled=False,
        service_name=DEFAULT_READ_POOLER_SERVICE,
    )

"""Render Postgres manifests: standalone Deployment or primary + read replicas."""

from __future__ import annotations

from typing import Any

from .models import (
    K8sWorkloadSpec,
    POSTGRES_DATABASE,
    POSTGRES_PASSWORD,
    POSTGRES_USER,
)
from .placement import _pod_spec_affinity, _postgres_container_args
from .postgres_tuning import postgres_tuning_bitnami_env

# Bitnami replication image. Short tags like ``bitnami/postgresql:17`` were removed
# from docker.io/bitnami (2025 catalog change); use the legacy repo for PG 17.
BITNAMI_POSTGRES_IMAGE = "bitnamilegacy/postgresql:17"
REPLICATION_USER = "replicator"
REPLICATION_PASSWORD = "replicator"


def _postgres_node_names(spec: K8sWorkloadSpec) -> tuple[str, ...]:
    if spec.database.placement_worker:
        return (spec.database.placement_worker,)
    if spec.database.placement_workers:
        return spec.database.placement_workers
    return ()


def _postgres_readiness_probe(
    *, bitnami: bool, replica: bool = False
) -> dict[str, Any]:
    user = POSTGRES_USER
    db = POSTGRES_DATABASE
    cmd = f"pg_isready -U {user} -d {db}"
    # Replicas run pg_basebackup from the primary on first boot; that can take
    # minutes for a non-trivial dataset and used to hit failureThreshold long
    # before the slave finished cloning.
    if replica and bitnami:
        return {
            "exec": {"command": ["sh", "-c", cmd]},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            "failureThreshold": 60,  # ~10 min before declared NotReady
        }
    return {
        "exec": {"command": ["sh", "-c", cmd]},
        "initialDelaySeconds": 10 if bitnami else 5,
        "periodSeconds": 5,
        "failureThreshold": 12 if bitnami else 6,
    }


def _standalone_postgres_manifests(
    spec: K8sWorkloadSpec,
    *,
    labels: dict[str, str],
    pg_nodes: tuple[str, ...],
) -> list[dict[str, Any]]:
    name = spec.database.service_name
    selector = {"app": name}
    pg_args = _postgres_container_args(spec)
    pod_spec = _pod_spec_affinity(spec, role="db", node_names=pg_nodes)
    container: dict[str, Any] = {
        "name": "postgres",
        "image": spec.database.image,
        "ports": [{"containerPort": spec.database.port}],
        "env": [
            {"name": "POSTGRES_USER", "value": POSTGRES_USER},
            {"name": "POSTGRES_PASSWORD", "value": POSTGRES_PASSWORD},
            {"name": "POSTGRES_DB", "value": POSTGRES_DATABASE},
        ],
        "resources": spec.database.effective_primary_resources().to_k8s_resources(),
        "readinessProbe": _postgres_readiness_probe(bitnami=False),
    }
    if pg_args:
        container["args"] = pg_args
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
                "replicas": 1,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": {**selector, **labels}},
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
                "ports": [
                    {"port": spec.database.port, "targetPort": spec.database.port}
                ],
            },
        },
    ]


def _bitnami_primary_env(spec: K8sWorkloadSpec) -> list[dict[str, str]]:
    env = [
        {"name": "POSTGRESQL_REPLICATION_MODE", "value": "master"},
        {"name": "POSTGRESQL_REPLICATION_USER", "value": REPLICATION_USER},
        {"name": "POSTGRESQL_REPLICATION_PASSWORD", "value": REPLICATION_PASSWORD},
        {"name": "POSTGRESQL_USERNAME", "value": POSTGRES_USER},
        {"name": "POSTGRESQL_PASSWORD", "value": POSTGRES_PASSWORD},
        {"name": "POSTGRESQL_DATABASE", "value": POSTGRES_DATABASE},
        {
            "name": "POSTGRESQL_MAX_CONNECTIONS",
            "value": str(spec.database.max_connections),
        },
    ]
    env.extend(postgres_tuning_bitnami_env(spec.database.tuning))
    return env


def _bitnami_replica_env(spec: K8sWorkloadSpec) -> list[dict[str, str]]:
    master_host = (
        f"{spec.database.service_name}.{spec.namespace}.svc.cluster.local"
    )
    env = [
        {"name": "POSTGRESQL_REPLICATION_MODE", "value": "slave"},
        {"name": "POSTGRESQL_MASTER_HOST", "value": master_host},
        {"name": "POSTGRESQL_MASTER_PORT_NUMBER", "value": str(spec.database.port)},
        {"name": "POSTGRESQL_REPLICATION_USER", "value": REPLICATION_USER},
        {"name": "POSTGRESQL_REPLICATION_PASSWORD", "value": REPLICATION_PASSWORD},
        {"name": "POSTGRESQL_USERNAME", "value": POSTGRES_USER},
        {"name": "POSTGRESQL_PASSWORD", "value": POSTGRES_PASSWORD},
        {"name": "POSTGRESQL_DATABASE", "value": POSTGRES_DATABASE},
        # Postgres streaming replication requires the standby's
        # ``max_connections`` to be **>=** the primary's, or recovery
        # aborts with ``FATAL: recovery aborted because of insufficient
        # parameter settings``. Mirror the primary's value so the replica
        # can ever enter standby mode.
        {
            "name": "POSTGRESQL_MAX_CONNECTIONS",
            "value": str(spec.database.max_connections),
        },
    ]
    env.extend(postgres_tuning_bitnami_env(spec.database.tuning))
    return env


def _replicated_postgres_manifests(
    spec: K8sWorkloadSpec,
    *,
    labels: dict[str, str],
    pg_nodes: tuple[str, ...],
) -> list[dict[str, Any]]:
    """
    Primary/replica layout (standard Postgres on Kubernetes):

    - One **primary** Deployment (writes + app connections via ``postgres`` Service)
    - ``replicas - 1`` **read replicas** in a StatefulSet streaming WAL from primary
    - Optional ``postgres-read`` Service (replicas are not used by the generated app)
    """
    name = spec.database.service_name
    read_count = max(0, spec.database.replicas - 1)
    primary_labels = {
        **labels,
        "app": name,
        "baxbench.dev/db-tier": "primary",
    }
    primary_selector = {"app": name, "baxbench.dev/db-tier": "primary"}
    pod_spec = _pod_spec_affinity(
        spec,
        role="db",
        node_names=pg_nodes,
        spread=read_count > 0,
    )
    primary_container: dict[str, Any] = {
        "name": "postgresql",
        "image": BITNAMI_POSTGRES_IMAGE,
        "ports": [{"containerPort": spec.database.port}],
        "env": _bitnami_primary_env(spec),
        "resources": spec.database.effective_primary_resources().to_k8s_resources(),
        "readinessProbe": _postgres_readiness_probe(bitnami=True),
    }
    docs: list[dict[str, Any]] = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": spec.namespace,
                "labels": primary_labels,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": primary_selector},
                "template": {
                    "metadata": {"labels": primary_labels},
                    "spec": {
                        **pod_spec,
                        "containers": [primary_container],
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
                "labels": primary_labels,
            },
            "spec": {
                "selector": primary_selector,
                "ports": [
                    {"port": spec.database.port, "targetPort": spec.database.port}
                ],
            },
        },
    ]
    if read_count == 0:
        return docs

    replica_name = f"{name}-replica"
    replica_labels = {
        **labels,
        "app": replica_name,
        "baxbench.dev/db-tier": "replica",
    }
    replica_selector = {"app": replica_name, "baxbench.dev/db-tier": "replica"}
    replica_pod_spec = _pod_spec_affinity(
        spec,
        role="db",
        node_names=pg_nodes,
        spread=True,
    )
    replica_container: dict[str, Any] = {
        "name": "postgresql",
        "image": BITNAMI_POSTGRES_IMAGE,
        "ports": [{"containerPort": spec.database.port}],
        "env": _bitnami_replica_env(spec),
        "resources": spec.database.effective_replica_resources().to_k8s_resources(),
        "readinessProbe": _postgres_readiness_probe(bitnami=True, replica=True),
    }
    docs.extend(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {
                    "name": replica_name,
                    "namespace": spec.namespace,
                    "labels": replica_labels,
                },
                "spec": {
                    "serviceName": f"{replica_name}-headless",
                    # Parallel: both replicas can clone from the primary
                    # concurrently. OrderedReady (the default) makes the wait
                    # time scale with replica count, which routinely blew
                    # past the deploy timeout in earlier runs.
                    "podManagementPolicy": "Parallel",
                    "replicas": read_count,
                    "selector": {"matchLabels": replica_selector},
                    "template": {
                        "metadata": {"labels": replica_labels},
                        "spec": {
                            **replica_pod_spec,
                            "containers": [replica_container],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": f"{replica_name}-headless",
                    "namespace": spec.namespace,
                    "labels": replica_labels,
                },
                "spec": {
                    "clusterIP": "None",
                    "selector": replica_selector,
                    "ports": [
                        {
                            "port": spec.database.port,
                            "targetPort": spec.database.port,
                        }
                    ],
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": f"{name}-read",
                    "namespace": spec.namespace,
                    "labels": replica_labels,
                },
                "spec": {
                    "selector": replica_selector,
                    "ports": [
                        {
                            "port": spec.database.port,
                            "targetPort": spec.database.port,
                        }
                    ],
                },
            },
        ]
    )
    return docs


def build_postgres_manifests(
    spec: K8sWorkloadSpec,
    *,
    common_labels: dict[str, str],
) -> list[dict[str, Any]]:
    labels = {**common_labels, "baxbench.dev/role": "db"}
    pg_nodes = _postgres_node_names(spec)
    if spec.database.replicas <= 1:
        return _standalone_postgres_manifests(spec, labels=labels, pg_nodes=pg_nodes)
    return _replicated_postgres_manifests(spec, labels=labels, pg_nodes=pg_nodes)

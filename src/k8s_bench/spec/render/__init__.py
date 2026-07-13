from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..models import K8sWorkloadSpec
from .pgbouncer import build_pgbouncer_manifests
from ..components.placement import _pod_spec_affinity
from .postgres import build_postgres_manifests
from .redis import build_redis_manifests
from ...workspace.paths import iteration_manifests_dir, iteration_spec_path


def _common_labels(spec: K8sWorkloadSpec) -> dict[str, str]:
    labels = {
        "app.kubernetes.io/managed-by": "baxbench-k8s-bench",
        "baxbench.dev/iteration": spec.iteration_id,
    }
    labels.update(spec.labels)
    return labels


def _namespace_manifest(spec: K8sWorkloadSpec) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": spec.namespace,
            "labels": _common_labels(spec),
        },
    }


def _postgres_manifests(spec: K8sWorkloadSpec) -> list[dict[str, Any]]:
    return build_postgres_manifests(spec, common_labels=_common_labels(spec))


def _backend_container(spec: K8sWorkloadSpec, *, port: int, env_list: list[dict[str, str]]) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "app",
        "image": spec.backend.image,
        "imagePullPolicy": (
            "Never"
            if spec.backend.image.startswith("baxbench-local/")
            else "IfNotPresent"
        ),
        "ports": [{"containerPort": port}],
        "env": env_list,
        "resources": spec.backend.resources.to_k8s_resources(),
        "readinessProbe": {
            "tcpSocket": {"port": port},
            "initialDelaySeconds": 3,
            "periodSeconds": 5,
        },
    }
    env_id = (spec.labels.get("baxbench.dev/env") or "").lower()
    if "flask" in env_id or env_id.startswith("python"):
        container["command"] = [
            "sh",
            "-c",
            "exec gunicorn --preload --workers=${WEB_CONCURRENCY:-2} "
            "--worker-class=sync "
            "--bind 0.0.0.0:${PORT:-5001} app:app",
        ]
    elif "express" in env_id or "javascript" in env_id or "node" in env_id:
        container["command"] = [
            "sh",
            "-c",
            "exec npx --no-install pm2-runtime start app.js -i ${WEB_CONCURRENCY:-max}",
        ]
    return container


def _backend_manifests(spec: K8sWorkloadSpec) -> list[dict[str, Any]]:
    name = "backend"
    labels = {**_common_labels(spec), "baxbench.dev/role": "app"}
    selector = {"app": name}
    env_list = [{"name": k, "value": v} for k, v in spec.backend_env().items()]
    port = spec.backend.port
    be_nodes = spec.backend.placement_workers
    pod_spec = _pod_spec_affinity(
        spec,
        role="backend",
        node_names=be_nodes,
        spread=spec.backend.spread_replicas,
    )
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
                "replicas": spec.backend.replicas,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": {**selector, **labels}},
                    "spec": {
                        **pod_spec,
                        "containers": [
                            _backend_container(spec, port=port, env_list=env_list),
                        ]
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
                "type": "NodePort",
                "selector": selector,
                "ports": [{"port": port, "targetPort": port}],
            },
        },
    ]


def build_manifest_documents(spec: K8sWorkloadSpec) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = [_namespace_manifest(spec)]
    if spec.database.enabled:
        docs.extend(_postgres_manifests(spec))
        docs.extend(build_pgbouncer_manifests(spec, common_labels=_common_labels(spec)))
    docs.extend(build_redis_manifests(spec, common_labels=_common_labels(spec)))
    docs.extend(_backend_manifests(spec))
    return docs


def write_manifest_files(
    spec: K8sWorkloadSpec,
    out_dir: Path,
    *,
    combined_filename: str = "all.yaml",
) -> Path:
    """
    Write generated manifest YAML files under ``out_dir``.

    Returns path to the combined multi-document YAML file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = build_manifest_documents(spec)
    combined_path = out_dir / combined_filename
    with open(combined_path, "w", encoding="utf-8") as f:
        yaml.safe_dump_all(docs, f, sort_keys=False, default_flow_style=False)
    for i, doc in enumerate(docs):
        kind = str(doc.get("kind", "resource")).lower()
        name = doc.get("metadata", {}).get("name", str(i))
        single = out_dir / f"{i:02d}-{kind}-{name}.yaml"
        single.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return combined_path


def render_manifests(iteration_path: Path) -> Path:
    """Render ``04-deploy/manifests/`` from the iteration's ``spec.yaml``."""
    spec = K8sWorkloadSpec.from_yaml_file(iteration_spec_path(iteration_path))
    return write_manifest_files(spec, iteration_manifests_dir(iteration_path))

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .postgres_tuning import PostgresTuningSpec

POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "postgres"
POSTGRES_DATABASE = "testdb"

# Default number of app worker processes per backend pod when a spec does not
# set ``backend.web_concurrency``. Propagated as the ``WEB_CONCURRENCY`` env var
# (gunicorn ``--workers`` for Python, PM2 ``-i`` for JavaScript).
DEFAULT_WEB_CONCURRENCY = 2


@dataclass(frozen=True)
class ResourceSpec:
    cpu_request: str = "250m"
    cpu_limit: str = "1"
    memory_request: str = "256Mi"
    memory_limit: str = "512Mi"

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> ResourceSpec:
        if not data:
            return cls()
        return cls(
            cpu_request=str(data.get("cpu_request", cls.cpu_request)),
            cpu_limit=str(data.get("cpu_limit", cls.cpu_limit)),
            memory_request=str(data.get("memory_request", cls.memory_request)),
            memory_limit=str(data.get("memory_limit", cls.memory_limit)),
        )

    def to_k8s_resources(self) -> dict[str, dict[str, str]]:
        return {
            "requests": {
                "cpu": self.cpu_request,
                "memory": self.memory_request,
            },
            "limits": {
                "cpu": self.cpu_limit,
                "memory": self.memory_limit,
            },
        }


@dataclass(frozen=True)
class BackendSpec:
    image: str
    replicas: int = 1
    port: int = 8080
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    # App worker processes per pod. Propagated as WEB_CONCURRENCY
    # (gunicorn --workers for Python, PM2 -i for JavaScript).
    web_concurrency: int = DEFAULT_WEB_CONCURRENCY
    # Env vars passed to the app container (DB_* added automatically when DB enabled).
    env: dict[str, str] = field(default_factory=dict)
    # Kubernetes node hostnames (from cluster capacity) allowed for backend pods.
    placement_workers: tuple[str, ...] = ()
    # When true, prefer spreading replicas across nodes (pod anti-affinity).
    spread_replicas: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> BackendSpec:
        placement_raw = data.get("placement") or {}
        workers: list[str] = []
        spread = True
        if isinstance(placement_raw, dict):
            raw_workers = placement_raw.get("workers") or placement_raw.get(
                "worker_nodes"
            ) or []
            if isinstance(raw_workers, (list, tuple)):
                workers = [str(w) for w in raw_workers if str(w).strip()]
            spread = bool(placement_raw.get("spread_replicas", True))
        return cls(
            image=str(data["image"]),
            replicas=int(data.get("replicas", 1)),
            port=int(data.get("port", 8080)),
            resources=ResourceSpec.from_mapping(data.get("resources")),
            web_concurrency=max(
                1,
                int(data.get("web_concurrency", DEFAULT_WEB_CONCURRENCY)),
            ),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            placement_workers=tuple(workers),
            spread_replicas=spread,
        )


@dataclass(frozen=True)
class DatabaseSpec:
    enabled: bool = True
    image: str = "postgres:17-alpine"
    service_name: str = "postgres"
    port: int = 5432
    # replicas=1: standalone Deployment. replicas>1: primary + (N-1) read replicas.
    replicas: int = 1
    max_connections: int = 100
    tuning: PostgresTuningSpec = field(default_factory=PostgresTuningSpec)
    # Exact pin (one node). Takes precedence over placement_workers when set.
    placement_worker: str | None = None
    # Allow-list: scheduler picks one node from this set (single postgres pod).
    placement_workers: tuple[str, ...] = ()
    resources: ResourceSpec = field(
        default_factory=lambda: ResourceSpec(
            cpu_request="500m",
            cpu_limit="2",
            memory_request="512Mi",
            memory_limit="2Gi",
        )
    )

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> DatabaseSpec:
        if not data:
            return cls()
        placement_raw = data.get("placement") or {}
        pin_worker: str | None = None
        workers: list[str] = []
        if isinstance(placement_raw, dict):
            w = placement_raw.get("worker") or placement_raw.get("worker_node")
            if w is not None and str(w).strip():
                pin_worker = str(w).strip()
            raw_workers = placement_raw.get("workers") or placement_raw.get(
                "worker_nodes"
            ) or []
            if isinstance(raw_workers, (list, tuple)):
                workers = [str(x).strip() for x in raw_workers if str(x).strip()]
        max_conn = int(data.get("max_connections", cls.max_connections))
        tuning_raw = data.get("tuning")
        tuning = (
            PostgresTuningSpec.from_mapping(tuning_raw)
            if isinstance(tuning_raw, dict)
            else PostgresTuningSpec()
        )
        return cls(
            enabled=bool(data.get("enabled", True)),
            image=str(data.get("image", cls.image)),
            service_name=str(data.get("service_name", cls.service_name)),
            port=int(data.get("port", cls.port)),
            replicas=max(1, int(data.get("replicas", cls.replicas))),
            max_connections=max(1, max_conn),
            tuning=tuning,
            placement_worker=pin_worker,
            placement_workers=tuple(workers),
            resources=ResourceSpec.from_mapping(data.get("resources")),
        )


def _database_placement_yaml(db: DatabaseSpec) -> dict[str, Any]:
    if db.placement_worker:
        return {"worker": db.placement_worker}
    if db.placement_workers:
        return {"workers": list(db.placement_workers)}
    return {}


@dataclass(frozen=True)
class K8sWorkloadSpec:
    """
    Source of truth for one agent/human iteration under ``iterations/iteration-NNN/spec/spec.yaml``.
    """

    iteration_id: str
    namespace: str
    backend: BackendSpec
    database: DatabaseSpec = field(default_factory=DatabaseSpec)
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml_file(cls, path: Path) -> K8sWorkloadSpec:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"spec must be a mapping: {path}")
        return cls.from_mapping(raw, iteration_id=path.parent.name)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, iteration_id: str) -> K8sWorkloadSpec:
        meta = raw.get("metadata") or {}
        iid = str(meta.get("iteration_id") or raw.get("iteration_id") or iteration_id)
        ns = str(raw.get("namespace") or meta.get("namespace") or f"baxbench-{iid}")
        backend_raw = raw.get("backend")
        if not isinstance(backend_raw, dict) or "image" not in backend_raw:
            raise ValueError("spec.backend.image is required")
        db_raw = raw.get("database")
        db = DatabaseSpec.from_mapping(db_raw if isinstance(db_raw, dict) else None)
        labels = raw.get("labels") or meta.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return cls(
            iteration_id=iid,
            namespace=ns,
            backend=BackendSpec.from_mapping(backend_raw),
            database=db,
            labels={str(k): str(v) for k, v in labels.items()},
        )

    def to_yaml_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": "baxbench.dev/v1alpha1",
            "kind": "K8sWorkloadSpec",
            "metadata": {"iteration_id": self.iteration_id},
            "namespace": self.namespace,
            "labels": dict(self.labels),
            "backend": {
                "image": self.backend.image,
                "replicas": self.backend.replicas,
                "port": self.backend.port,
                "web_concurrency": self.backend.web_concurrency,
                "resources": asdict(self.backend.resources),
                "env": dict(self.backend.env),
                "placement": {
                    "workers": list(self.backend.placement_workers),
                    "spread_replicas": self.backend.spread_replicas,
                },
            },
            "database": {
                "enabled": self.database.enabled,
                "image": self.database.image,
                "service_name": self.database.service_name,
                "port": self.database.port,
                "replicas": self.database.replicas,
                "max_connections": self.database.max_connections,
                **(
                    {"tuning": self.database.tuning.to_mapping()}
                    if not self.database.tuning.is_empty()
                    else {}
                ),
                "placement": _database_placement_yaml(self.database),
                "resources": asdict(self.database.resources),
            },
        }

    def write_yaml(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.to_yaml_dict(), sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

    def backend_env(self) -> dict[str, str]:
        env = dict(self.backend.env)
        env.setdefault("PORT", str(self.backend.port))
        env.setdefault("WEB_CONCURRENCY", str(self.backend.web_concurrency))
        if self.database.enabled:
            host = f"{self.database.service_name}.{self.namespace}.svc.cluster.local"
            env.setdefault("DB_HOST", host)
            env.setdefault("DB_PORT", str(self.database.port))
            env.setdefault("DB_USER", POSTGRES_USER)
            env.setdefault("DB_PASSWORD", POSTGRES_PASSWORD)
            env.setdefault("DB_NAME", POSTGRES_DATABASE)
            # When replicas exist, expose the read-only Service so the app can
            # route SELECTs to replicas (apps that ignore this var keep using
            # DB_HOST and behave exactly as before — opt-in via code refinement).
            if self.database.replicas > 1:
                read_host = (
                    f"{self.database.service_name}-read."
                    f"{self.namespace}.svc.cluster.local"
                )
                env.setdefault("DB_READ_HOST", read_host)
        return env

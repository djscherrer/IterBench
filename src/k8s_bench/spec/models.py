from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .components import (
    CacheSpec,
    DatabaseCacheSpec,
    DEFAULT_READ_POOLER_SERVICE,
    PoolerSpec,
    PostgresTuningSpec,
)

POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "postgres"
POSTGRES_DATABASE = "testdb"

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
    env: dict[str, str] = field(default_factory=dict)
    # Kubernetes node hostnames (from cluster capacity) allowed for backend pods.
    placement_workers: tuple[str, ...] = ()
    # When true, prefer spreading replicas across nodes (pod anti-affinity).
    spread_replicas: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> BackendSpec:
        from .components import parse_backend_env

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
        env, _env_errors = parse_backend_env(data.get("env"))
        return cls(
            image=str(data["image"]),
            replicas=int(data.get("replicas", 1)),
            port=int(data.get("port", 8080)),
            resources=ResourceSpec.from_mapping(data.get("resources")),
            env=env,
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
    # Default resources for all DB pods when primary/replica overrides are unset.
    resources: ResourceSpec = field(
        default_factory=lambda: ResourceSpec(
            cpu_request="500m",
            cpu_limit="2",
            memory_request="512Mi",
            memory_limit="2Gi",
        )
    )
    # Optional per-tier overrides (primary write path vs read replicas).
    primary_resources: ResourceSpec | None = None
    replica_resources: ResourceSpec | None = None
    cache: DatabaseCacheSpec = field(default_factory=DatabaseCacheSpec)

    def effective_primary_resources(self) -> ResourceSpec:
        return self.primary_resources or self.resources

    def effective_replica_resources(self) -> ResourceSpec:
        return self.replica_resources or self.resources

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
        primary_raw = data.get("primary")
        replica_raw = data.get("replica")
        primary_resources: ResourceSpec | None = None
        replica_resources: ResourceSpec | None = None
        if isinstance(primary_raw, dict) and primary_raw.get("resources"):
            primary_resources = ResourceSpec.from_mapping(primary_raw.get("resources"))
        if isinstance(replica_raw, dict) and replica_raw.get("resources"):
            replica_resources = ResourceSpec.from_mapping(replica_raw.get("resources"))
        cache_raw = data.get("cache")
        db_cache = (
            DatabaseCacheSpec.from_mapping(cache_raw)
            if isinstance(cache_raw, dict)
            else DatabaseCacheSpec()
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
            primary_resources=primary_resources,
            replica_resources=replica_resources,
            cache=db_cache,
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
    pooler: PoolerSpec = field(default_factory=PoolerSpec)
    read_pooler: PoolerSpec = field(
        default_factory=lambda: PoolerSpec(
            enabled=False, service_name=DEFAULT_READ_POOLER_SERVICE
        )
    )
    cache: CacheSpec = field(default_factory=CacheSpec)
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
        pooler_raw = raw.get("pooler")
        pooler = (
            PoolerSpec.from_mapping(pooler_raw)
            if isinstance(pooler_raw, dict)
            else PoolerSpec()
        )
        read_pooler_raw = raw.get("read_pooler")
        read_pooler = (
            PoolerSpec.from_mapping(
                read_pooler_raw, default_service_name=DEFAULT_READ_POOLER_SERVICE
            )
            if isinstance(read_pooler_raw, dict)
            else PoolerSpec(enabled=False, service_name=DEFAULT_READ_POOLER_SERVICE)
        )
        cache_raw = raw.get("cache")
        cache = (
            CacheSpec.from_mapping(cache_raw)
            if isinstance(cache_raw, dict)
            else CacheSpec()
        )
        labels = raw.get("labels") or meta.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return cls(
            iteration_id=iid,
            namespace=ns,
            backend=BackendSpec.from_mapping(backend_raw),
            database=db,
            pooler=pooler,
            read_pooler=read_pooler,
            cache=cache,
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
                "resources": asdict(self.backend.resources),
                **({"env": dict(self.backend.env)} if self.backend.env else {}),
                "placement": {
                    "workers": list(self.backend.placement_workers),
                    "spread_replicas": self.backend.spread_replicas,
                },
            },
            **(
                {"pooler": self.pooler.to_mapping()}
                if not self.pooler.is_empty()
                else {}
            ),
            **(
                {"read_pooler": self.read_pooler.to_mapping()}
                if not self.read_pooler.is_empty()
                else {}
            ),
            **(
                {"cache": self.cache.to_mapping()}
                if not self.cache.is_empty()
                else {}
            ),
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
                **(
                    {"primary": {"resources": asdict(self.database.primary_resources)}}
                    if self.database.primary_resources is not None
                    else {}
                ),
                **(
                    {"replica": {"resources": asdict(self.database.replica_resources)}}
                    if self.database.replica_resources is not None
                    else {}
                ),
                **(
                    {"cache": self.database.cache.to_mapping()}
                    if not self.database.cache.is_empty()
                    else {}
                ),
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
        if self.cache.enabled:
            env.setdefault("REDIS_URL", self.cache.redis_url(self.namespace))
        if self.database.cache.enabled:
            if self.database.cache.use_shared and self.cache.enabled:
                env.setdefault("DB_REDIS_URL", self.cache.redis_url(self.namespace))
            elif not self.database.cache.use_shared:
                env.setdefault(
                    "DB_REDIS_URL",
                    self.database.cache.redis_url(self.namespace),
                )
        if self.database.enabled:
            if self.pooler.enabled:
                host = f"{self.pooler.service_name}.{self.namespace}.svc.cluster.local"
                db_port = str(self.pooler.port)
            else:
                host = (
                    f"{self.database.service_name}.{self.namespace}.svc.cluster.local"
                )
                db_port = str(self.database.port)
            env.setdefault("DB_HOST", host)
            env.setdefault("DB_PORT", db_port)
            env.setdefault("DB_USER", POSTGRES_USER)
            env.setdefault("DB_PASSWORD", POSTGRES_PASSWORD)
            env.setdefault("DB_NAME", POSTGRES_DATABASE)
            if self.database.replicas > 1:
                if self.read_pooler.enabled:
                    read_host = (
                        f"{self.read_pooler.service_name}."
                        f"{self.namespace}.svc.cluster.local"
                    )
                    env.setdefault("DB_READ_HOST", read_host)
                    env.setdefault("DB_READ_PORT", str(self.read_pooler.port))
                else:
                    read_host = (
                        f"{self.database.service_name}-read."
                        f"{self.namespace}.svc.cluster.local"
                    )
                    env.setdefault("DB_READ_HOST", read_host)
                    env.setdefault("DB_READ_PORT", str(self.database.port))
        return env

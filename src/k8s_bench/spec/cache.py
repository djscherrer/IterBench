"""Optional Redis cache tier for app / database-adjacent caching."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ResourceSpec

DEFAULT_REDIS_IMAGE = "redis:7-alpine"
DEFAULT_REDIS_SERVICE = "redis"
DEFAULT_DB_REDIS_SERVICE = "redis-db"
DEFAULT_REDIS_PORT = 6379


def quantity_to_redis_maxmemory(quantity: str) -> str:
    """Convert a Kubernetes quantity (e.g. ``256Mi``) to bytes for ``redis-server --maxmemory``."""
    from ..cluster.capacity import _parse_memory_to_bytes

    nbytes = _parse_memory_to_bytes(quantity)
    if nbytes <= 0:
        raise ValueError(f"invalid Redis maxmemory quantity: {quantity!r}")
    return str(nbytes)
ALLOWED_EVICTION_POLICIES = frozenset(
    {
        "allkeys-lru",
        "volatile-lru",
        "allkeys-lfu",
        "volatile-lfu",
        "allkeys-random",
        "volatile-random",
        "volatile-ttl",
        "noeviction",
    }
)


def _default_cache_resources() -> ResourceSpec:
    from .models import ResourceSpec

    return ResourceSpec(
        cpu_request="100m",
        cpu_limit="500m",
        memory_request="128Mi",
        memory_limit="512Mi",
    )


@dataclass(frozen=True)
class CacheSpec:
    """
    Optional Redis Deployment for application caching.

    When enabled, backends receive ``REDIS_URL`` (unless only database cache
  is used — see :class:`DatabaseCacheSpec`).
    """

    enabled: bool = False
    service_name: str = DEFAULT_REDIS_SERVICE
    port: int = DEFAULT_REDIS_PORT
    replicas: int = 1
    maxmemory: str = "256Mi"
    maxmemory_policy: str = "allkeys-lru"
    resources: ResourceSpec = field(default_factory=_default_cache_resources)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> CacheSpec:
        from .models import ResourceSpec

        if not data:
            return cls()
        policy = str(data.get("maxmemory_policy", "allkeys-lru")).strip()
        if policy not in ALLOWED_EVICTION_POLICIES:
            policy = "allkeys-lru"
        return cls(
            enabled=bool(data.get("enabled", False)),
            service_name=str(data.get("service_name", DEFAULT_REDIS_SERVICE)),
            port=int(data.get("port", DEFAULT_REDIS_PORT)),
            replicas=max(1, int(data.get("replicas", 1))),
            maxmemory=str(data.get("maxmemory", "256Mi")),
            maxmemory_policy=policy,
            resources=(
                ResourceSpec.from_mapping(data.get("resources"))
                if data.get("resources")
                else _default_cache_resources()
            ),
        )

    def is_empty(self) -> bool:
        return not self.enabled

    def to_mapping(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        out: dict[str, Any] = {
            "enabled": True,
            "maxmemory": self.maxmemory,
            "maxmemory_policy": self.maxmemory_policy,
        }
        if self.replicas != 1:
            out["replicas"] = self.replicas
        if self.service_name != DEFAULT_REDIS_SERVICE:
            out["service_name"] = self.service_name
        if self.port != DEFAULT_REDIS_PORT:
            out["port"] = self.port
        res = asdict(self.resources)
        default_res = asdict(_default_cache_resources())
        if res != default_res:
            out["resources"] = res
        return out

    def redis_url(self, namespace: str) -> str:
        host = f"{self.service_name}.{namespace}.svc.cluster.local"
        return f"redis://{host}:{self.port}/0"


@dataclass(frozen=True)
class DatabaseCacheSpec:
    """
    Optional Redis for database-adjacent caching (query-result cache, etc.).

    When ``use_shared`` is true and the root :class:`CacheSpec` is enabled,
    ``DB_REDIS_URL`` points at the same Redis Service as ``REDIS_URL``.
    Otherwise a dedicated ``redis-db`` Deployment is rendered.
    """

    enabled: bool = False
    use_shared: bool = True
    service_name: str = DEFAULT_DB_REDIS_SERVICE
    port: int = DEFAULT_REDIS_PORT
    replicas: int = 1
    maxmemory: str = "256Mi"
    maxmemory_policy: str = "allkeys-lru"
    resources: ResourceSpec = field(default_factory=_default_cache_resources)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> DatabaseCacheSpec:
        from .models import ResourceSpec

        if not data:
            return cls()
        policy = str(data.get("maxmemory_policy", "allkeys-lru")).strip()
        if policy not in ALLOWED_EVICTION_POLICIES:
            policy = "allkeys-lru"
        return cls(
            enabled=bool(data.get("enabled", False)),
            use_shared=bool(data.get("use_shared", True)),
            service_name=str(data.get("service_name", DEFAULT_DB_REDIS_SERVICE)),
            port=int(data.get("port", DEFAULT_REDIS_PORT)),
            replicas=max(1, int(data.get("replicas", 1))),
            maxmemory=str(data.get("maxmemory", "256Mi")),
            maxmemory_policy=policy,
            resources=(
                ResourceSpec.from_mapping(data.get("resources"))
                if data.get("resources")
                else _default_cache_resources()
            ),
        )

    def is_empty(self) -> bool:
        return not self.enabled

    def to_mapping(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        out: dict[str, Any] = {"enabled": True}
        if not self.use_shared:
            out["use_shared"] = False
            out["maxmemory"] = self.maxmemory
            out["maxmemory_policy"] = self.maxmemory_policy
            if self.replicas != 1:
                out["replicas"] = self.replicas
            if self.service_name != DEFAULT_DB_REDIS_SERVICE:
                out["service_name"] = self.service_name
            res = asdict(self.resources)
            default_res = asdict(_default_cache_resources())
            if res != default_res:
                out["resources"] = res
        return out

    def redis_url(self, namespace: str) -> str:
        host = f"{self.service_name}.{namespace}.svc.cluster.local"
        return f"redis://{host}:{self.port}/0"


def validate_cache(cache: CacheSpec) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not cache.enabled:
        return errors, warnings
    if cache.replicas < 1:
        errors.append("cache.replicas must be >= 1")
    return errors, warnings


def validate_database_cache(
    db_cache: DatabaseCacheSpec,
    *,
    backend_cache: CacheSpec,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not db_cache.enabled:
        return errors, warnings
    if db_cache.use_shared and not backend_cache.enabled:
        errors.append(
            "database.cache.use_shared requires cache.enabled=true on the backend "
            "cache tier (or set database.cache.use_shared: false for a dedicated "
            "redis-db Deployment)"
        )
    if not db_cache.use_shared and db_cache.replicas < 1:
        errors.append("database.cache.replicas must be >= 1")
    return errors, warnings

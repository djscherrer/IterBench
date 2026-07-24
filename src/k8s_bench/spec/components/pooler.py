"""PgBouncer pooler spec and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import ResourceSpec

PoolerMode = Literal["transaction", "session"]

PGBOUNCER_IMAGE = "edoburu/pgbouncer:1.22.1-p0"
DEFAULT_POOLER_SERVICE = "pgbouncer"
DEFAULT_READ_POOLER_SERVICE = "pgbouncer-read"
DEFAULT_POOLER_PORT = 6432


def _default_pooler_resources() -> ResourceSpec:
    from ..models import ResourceSpec

    return ResourceSpec(
        cpu_request="250m",
        cpu_limit="1",
        memory_request="128Mi",
        memory_limit="256Mi",
    )


@dataclass(frozen=True)
class PoolerSpec:
    """
    Optional PgBouncer sidecar tier between app pods and Postgres primary.

    When enabled, ``DB_HOST`` / ``DB_PORT`` on backends point at the pooler
    Service; the pooler opens a small server-side pool to the primary.
    """

    enabled: bool = False
    mode: PoolerMode = "transaction"
    service_name: str = DEFAULT_POOLER_SERVICE
    port: int = DEFAULT_POOLER_PORT
    replicas: int = 1
    max_client_conn: int = 2000
    default_pool_size: int = 50
    min_pool_size: int | None = None
    reserve_pool_size: int | None = None
    resources: ResourceSpec = field(default_factory=_default_pooler_resources)

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any] | None,
        *,
        default_service_name: str = DEFAULT_POOLER_SERVICE,
    ) -> PoolerSpec:
        from ..models import ResourceSpec

        if not data:
            return cls(service_name=default_service_name)
        mode_raw = str(data.get("mode", "transaction")).strip().lower()
        mode: PoolerMode = "session" if mode_raw == "session" else "transaction"

        def _optional_int(key: str) -> int | None:
            raw = data.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            return max(0, int(raw))

        return cls(
            enabled=bool(data.get("enabled", False)),
            mode=mode,
            service_name=str(data.get("service_name", default_service_name)),
            port=int(data.get("port", DEFAULT_POOLER_PORT)),
            replicas=max(1, int(data.get("replicas", 1))),
            max_client_conn=max(1, int(data.get("max_client_conn", 2000))),
            default_pool_size=max(1, int(data.get("default_pool_size", 50))),
            min_pool_size=_optional_int("min_pool_size"),
            reserve_pool_size=_optional_int("reserve_pool_size"),
            resources=(
                ResourceSpec.from_mapping(data.get("resources"))
                if data.get("resources")
                else _default_pooler_resources()
            ),
        )

    def is_empty(self) -> bool:
        return not self.enabled

    def to_mapping(self) -> dict[str, Any]:
        from ..models import ResourceSpec

        if not self.enabled:
            return {"enabled": False}
        out: dict[str, Any] = {
            "enabled": True,
            "mode": self.mode,
            "max_client_conn": self.max_client_conn,
            "default_pool_size": self.default_pool_size,
        }
        if self.replicas != 1:
            out["replicas"] = self.replicas
        if self.min_pool_size is not None:
            out["min_pool_size"] = self.min_pool_size
        if self.reserve_pool_size is not None:
            out["reserve_pool_size"] = self.reserve_pool_size
        if self.service_name != DEFAULT_POOLER_SERVICE:
            out["service_name"] = self.service_name
        if self.port != DEFAULT_POOLER_PORT:
            out["port"] = self.port
        res = asdict(self.resources)
        if res != asdict(
            ResourceSpec(
                cpu_request="250m",
                cpu_limit="1",
                memory_request="128Mi",
                memory_limit="256Mi",
            )
        ):
            out["resources"] = res
        return out


def validate_pooler(
    pooler: PoolerSpec,
    *,
    max_connections: int,
    client_connections_needed: int,
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for pooler vs app + Postgres limits."""
    errors: list[str] = []
    warnings: list[str] = []
    if not pooler.enabled:
        return errors, warnings

    if pooler.replicas < 1:
        errors.append("pooler.replicas must be >= 1")

    if pooler.mode not in ("transaction", "session"):
        errors.append("pooler.mode must be 'transaction' or 'session'")

    if client_connections_needed > pooler.max_client_conn:
        errors.append(
            f"pooler.max_client_conn={pooler.max_client_conn} is too low for "
            f"estimated app client connections ({client_connections_needed}). "
            "Raise max_client_conn or lower backend replicas / "
            "DB_POOL_SIZE."
        )

    if pooler.default_pool_size > max_connections:
        errors.append(
            f"pooler.default_pool_size={pooler.default_pool_size} exceeds "
            f"database.max_connections={max_connections}. "
            "PgBouncer cannot open more server connections than Postgres allows."
        )
    elif pooler.default_pool_size > max_connections * 0.8:
        warnings.append(
            "pooler.default_pool_size is >80% of database.max_connections — "
            "leave headroom for admin/replication connections."
        )

    if pooler.min_pool_size is not None:
        if pooler.min_pool_size > pooler.default_pool_size:
            errors.append(
                "pooler.min_pool_size cannot exceed pooler.default_pool_size"
            )
        elif pooler.min_pool_size < 0:
            errors.append("pooler.min_pool_size must be >= 0")

    if pooler.reserve_pool_size is not None and pooler.reserve_pool_size < 0:
        errors.append("pooler.reserve_pool_size must be >= 0")

    if pooler.mode == "session":
        warnings.append(
            "pooler.mode=session binds one Postgres connection per client session; "
            "multiplexing benefit is limited vs transaction mode."
        )

    return errors, warnings

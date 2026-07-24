"""Postgres GUC tuning from agent-controlled spec fields."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...cluster.capacity import _parse_memory_to_bytes

_MEMORY_QUANTITY_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T|KB|MB|GB|TB)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PostgresTuningSpec:
    """Optional Postgres configuration overrides (primary + replicas)."""

    shared_buffers: str | None = None
    effective_cache_size: str | None = None
    work_mem: str | None = None
    maintenance_work_mem: str | None = None
    max_parallel_workers_per_gather: int | None = None
    max_parallel_workers: int | None = None
    max_worker_processes: int | None = None
    random_page_cost: float | None = None
    effective_io_concurrency: int | None = None
    max_wal_size: str | None = None
    checkpoint_timeout_s: int | None = None
    wal_buffers: str | None = None
    jit_enabled: bool | None = None
    statement_timeout_ms: int | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> PostgresTuningSpec:
        if not data:
            return cls()

        def _optional_int(key: str) -> int | None:
            raw = data.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            return max(0, int(raw))

        def _optional_float(key: str) -> float | None:
            raw = data.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            return float(raw)

        def _optional_bool(key: str) -> bool | None:
            raw = data.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"true", "1", "yes", "on"}:
                return True
            if text in {"false", "0", "no", "off"}:
                return False
            return None

        return cls(
            shared_buffers=_optional_quantity(data.get("shared_buffers")),
            effective_cache_size=_optional_quantity(data.get("effective_cache_size")),
            work_mem=_optional_quantity(data.get("work_mem")),
            maintenance_work_mem=_optional_quantity(data.get("maintenance_work_mem")),
            max_parallel_workers_per_gather=_optional_int(
                "max_parallel_workers_per_gather"
            ),
            max_parallel_workers=_optional_int("max_parallel_workers"),
            max_worker_processes=_optional_int("max_worker_processes"),
            random_page_cost=_optional_float("random_page_cost"),
            effective_io_concurrency=_optional_int("effective_io_concurrency"),
            max_wal_size=_optional_quantity(data.get("max_wal_size")),
            checkpoint_timeout_s=_optional_int("checkpoint_timeout")
            or _optional_int("checkpoint_timeout_s"),
            wal_buffers=_optional_quantity(data.get("wal_buffers")),
            jit_enabled=_optional_bool("jit_enabled")
            if "jit_enabled" in data
            else _optional_bool("jit"),
            statement_timeout_ms=_optional_int("statement_timeout_ms")
            or _optional_int("statement_timeout"),
        )

    def is_empty(self) -> bool:
        return (
            self.shared_buffers is None
            and self.effective_cache_size is None
            and self.work_mem is None
            and self.maintenance_work_mem is None
            and self.max_parallel_workers_per_gather is None
            and self.max_parallel_workers is None
            and self.max_worker_processes is None
            and self.random_page_cost is None
            and self.effective_io_concurrency is None
            and self.max_wal_size is None
            and self.checkpoint_timeout_s is None
            and self.wal_buffers is None
            and self.jit_enabled is None
            and self.statement_timeout_ms is None
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.shared_buffers is not None:
            out["shared_buffers"] = self.shared_buffers
        if self.effective_cache_size is not None:
            out["effective_cache_size"] = self.effective_cache_size
        if self.work_mem is not None:
            out["work_mem"] = self.work_mem
        if self.maintenance_work_mem is not None:
            out["maintenance_work_mem"] = self.maintenance_work_mem
        if self.max_parallel_workers_per_gather is not None:
            out["max_parallel_workers_per_gather"] = self.max_parallel_workers_per_gather
        if self.max_parallel_workers is not None:
            out["max_parallel_workers"] = self.max_parallel_workers
        if self.max_worker_processes is not None:
            out["max_worker_processes"] = self.max_worker_processes
        if self.random_page_cost is not None:
            out["random_page_cost"] = self.random_page_cost
        if self.effective_io_concurrency is not None:
            out["effective_io_concurrency"] = self.effective_io_concurrency
        if self.max_wal_size is not None:
            out["max_wal_size"] = self.max_wal_size
        if self.checkpoint_timeout_s is not None:
            out["checkpoint_timeout"] = self.checkpoint_timeout_s
        if self.wal_buffers is not None:
            out["wal_buffers"] = self.wal_buffers
        if self.jit_enabled is not None:
            out["jit_enabled"] = self.jit_enabled
        if self.statement_timeout_ms is not None:
            out["statement_timeout_ms"] = self.statement_timeout_ms
        return out


def _optional_quantity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def quantity_to_postgres_memory(quantity: str) -> str:
    """
    Convert Kubernetes-style quantities (``256Mi``, ``1Gi``) to Postgres units.

    Postgres accepts ``kB``, ``MB``, and ``GB`` suffixes on GUC memory settings.
    """
    text = quantity.strip()
    match = _MEMORY_QUANTITY_RE.match(text)
    if not match:
        raise ValueError(f"invalid memory quantity: {quantity!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "").upper()
    if unit in {"", "B"}:
        if amount >= 1024**3:
            return f"{int(round(amount / (1024**3)))}GB"
        if amount >= 1024**2:
            return f"{int(round(amount / (1024**2)))}MB"
        if amount >= 1024:
            return f"{int(round(amount / 1024))}kB"
        return f"{int(round(amount))}B"
    if unit in {"K", "KB"}:
        return f"{int(round(amount))}kB"
    if unit in {"M", "MB"}:
        return f"{int(round(amount))}MB"
    if unit in {"G", "GB"}:
        return f"{int(round(amount))}GB"
    if unit in {"T", "TB"}:
        return f"{int(round(amount * 1024))}GB"
    if unit == "KI":
        return f"{int(round(amount))}kB"
    if unit == "MI":
        return f"{int(round(amount))}MB"
    if unit == "GI":
        return f"{int(round(amount))}GB"
    if unit == "TI":
        return f"{int(round(amount * 1024))}GB"
    raise ValueError(f"unsupported memory unit in quantity: {quantity!r}")


def postgres_tuning_guc_pairs(tuning: PostgresTuningSpec) -> list[tuple[str, str]]:
    """Return ``(guc_name, postgres_value)`` pairs for non-empty tuning fields."""
    pairs: list[tuple[str, str]] = []
    if tuning.shared_buffers is not None:
        pairs.append(
            ("shared_buffers", quantity_to_postgres_memory(tuning.shared_buffers))
        )
    if tuning.effective_cache_size is not None:
        pairs.append(
            (
                "effective_cache_size",
                quantity_to_postgres_memory(tuning.effective_cache_size),
            )
        )
    if tuning.work_mem is not None:
        pairs.append(("work_mem", quantity_to_postgres_memory(tuning.work_mem)))
    if tuning.maintenance_work_mem is not None:
        pairs.append(
            (
                "maintenance_work_mem",
                quantity_to_postgres_memory(tuning.maintenance_work_mem),
            )
        )
    if tuning.max_parallel_workers_per_gather is not None:
        pairs.append(
            (
                "max_parallel_workers_per_gather",
                str(tuning.max_parallel_workers_per_gather),
            )
        )
    if tuning.max_parallel_workers is not None:
        pairs.append(
            ("max_parallel_workers", str(tuning.max_parallel_workers))
        )
    if tuning.max_worker_processes is not None:
        pairs.append(
            ("max_worker_processes", str(tuning.max_worker_processes))
        )
    if tuning.random_page_cost is not None:
        pairs.append(("random_page_cost", str(tuning.random_page_cost)))
    if tuning.effective_io_concurrency is not None:
        pairs.append(
            (
                "effective_io_concurrency",
                str(tuning.effective_io_concurrency),
            )
        )
    if tuning.max_wal_size is not None:
        pairs.append(
            ("max_wal_size", quantity_to_postgres_memory(tuning.max_wal_size))
        )
    if tuning.checkpoint_timeout_s is not None:
        pairs.append(
            ("checkpoint_timeout", f"{tuning.checkpoint_timeout_s}s")
        )
    if tuning.wal_buffers is not None:
        pairs.append(
            ("wal_buffers", quantity_to_postgres_memory(tuning.wal_buffers))
        )
    if tuning.jit_enabled is not None:
        pairs.append(("jit", "on" if tuning.jit_enabled else "off"))
    if tuning.statement_timeout_ms is not None:
        pairs.append(
            ("statement_timeout", str(tuning.statement_timeout_ms))
        )
    return pairs


def postgres_tuning_container_args(tuning: PostgresTuningSpec) -> list[str]:
    """``postgres`` image CLI args: repeated ``-c name=value``."""
    args: list[str] = []
    for name, value in postgres_tuning_guc_pairs(tuning):
        args.extend(["-c", f"{name}={value}"])
    return args


def postgres_tuning_bitnami_env(tuning: PostgresTuningSpec) -> list[dict[str, str]]:
    """Bitnami image env vars for the same GUCs."""
    env: list[dict[str, str]] = []
    for name, value in postgres_tuning_guc_pairs(tuning):
        env_name = "POSTGRESQL_" + name.upper()
        env.append({"name": env_name, "value": value})
    return env


def validate_postgres_tuning(
    tuning: PostgresTuningSpec,
    *,
    memory_limit: str,
    max_connections: int,
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for tuning vs pod memory budget."""
    errors: list[str] = []
    warnings: list[str] = []
    mem_limit_bytes = _parse_memory_to_bytes(memory_limit)

    for field_name in (
        "shared_buffers",
        "effective_cache_size",
        "work_mem",
        "maintenance_work_mem",
        "max_wal_size",
        "wal_buffers",
    ):
        raw = getattr(tuning, field_name)
        if raw is None:
            continue
        try:
            quantity_to_postgres_memory(raw)
        except ValueError as exc:
            errors.append(f"database.tuning.{field_name}: {exc}")
            continue

    if tuning.shared_buffers is not None and mem_limit_bytes > 0:
        try:
            shared_bytes = _parse_memory_to_bytes(tuning.shared_buffers)
        except ValueError:
            pass
        else:
            if shared_bytes > mem_limit_bytes:
                errors.append(
                    "database.tuning.shared_buffers exceeds primary "
                    "memory_limit — Postgres cannot allocate more shared_buffers "
                    "than the pod memory limit."
                )
            elif shared_bytes > mem_limit_bytes * 0.4:
                warnings.append(
                    "database.tuning.shared_buffers is >40% of primary memory "
                    "limit — leave headroom for connections and OS cache."
                )

    if (
        tuning.work_mem is not None
        and tuning.max_parallel_workers_per_gather is not None
        and max_connections > 0
    ):
        try:
            work_bytes = _parse_memory_to_bytes(tuning.work_mem)
        except ValueError:
            work_bytes = 0
        parallel = max(1, tuning.max_parallel_workers_per_gather)
        worst_case = work_bytes * max_connections * parallel
        if worst_case > mem_limit_bytes * 4:
            warnings.append(
                "database.tuning.work_mem may be high for "
                f"max_connections={max_connections} and "
                f"max_parallel_workers_per_gather={parallel} — "
                "many concurrent queries can exhaust pod memory."
            )

    for field_name in (
        "max_parallel_workers_per_gather",
        "max_parallel_workers",
        "max_worker_processes",
        "effective_io_concurrency",
    ):
        value = getattr(tuning, field_name)
        if value is not None and value < 0:
            errors.append(f"database.tuning.{field_name} must be >= 0")

    if tuning.random_page_cost is not None and tuning.random_page_cost <= 0:
        errors.append("database.tuning.random_page_cost must be > 0")

    if tuning.checkpoint_timeout_s is not None and tuning.checkpoint_timeout_s <= 0:
        errors.append("database.tuning.checkpoint_timeout must be > 0")

    if tuning.statement_timeout_ms is not None and tuning.statement_timeout_ms < 0:
        errors.append("database.tuning.statement_timeout_ms must be >= 0")

    if (
        tuning.max_parallel_workers is not None
        and tuning.max_parallel_workers_per_gather is not None
        and tuning.max_parallel_workers_per_gather > tuning.max_parallel_workers
    ):
        errors.append(
            "database.tuning.max_parallel_workers_per_gather cannot exceed "
            "database.tuning.max_parallel_workers"
        )

    if (
        tuning.max_worker_processes is not None
        and tuning.max_parallel_workers is not None
        and tuning.max_parallel_workers > tuning.max_worker_processes
    ):
        errors.append(
            "database.tuning.max_parallel_workers cannot exceed "
            "database.tuning.max_worker_processes"
        )

    return errors, warnings

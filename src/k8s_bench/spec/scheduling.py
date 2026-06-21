"""Per-node scheduling validation for K8s workload specs."""

from __future__ import annotations

from dataclasses import dataclass

from ..cluster.capacity import (
    ClusterCapacity,
    NodeCapacity,
    _parse_cpu_to_millicores,
    _parse_memory_to_bytes,
)
from .models import K8sWorkloadSpec, ResourceSpec
from .postgres_tuning import validate_postgres_tuning
from .pooler import validate_pooler
from .cache import validate_cache, validate_database_cache
from .backend_env import parse_backend_env

DEFAULT_APP_POOL_MAX = 20
_NODE_RESERVE_FRACTION = 0.10


@dataclass(frozen=True)
class SpecValidationResult:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class SpecValidationError(ValueError):
    """Hard scheduling / capacity violations; safe to feed back to the LLM."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))

    def to_prompt_text(self) -> str:
        lines = [
            "## Spec validation failed (fix these before deploy)",
            "",
            "The previous YAML could not be scheduled on this cluster:",
            "",
        ]
        lines.extend(f"- {e}" for e in self.errors)
        lines.extend(
            [
                "",
                "Remember: **each pod** must fit on **one worker** using **requests**. "
                "Postgres primary is one pod; read replicas are separate pods (one node each).",
            ]
        )
        return "\n".join(lines)


def resolve_worker_node_name(name: str, capacity: ClusterCapacity) -> str | None:
    """Match short names (``node3``) or full ``kubernetes.io/hostname`` values."""
    needle = (name or "").strip()
    if not needle:
        return None
    workers = list(capacity.worker_nodes)
    for w in workers:
        if w.name == needle:
            return w.name
    short = needle.split(".")[0]
    matches = [w.name for w in workers if w.name == short or w.name.startswith(f"{short}.")]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    return None


def normalize_spec_placement(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> tuple[K8sWorkloadSpec, list[str]]:
    """Resolve placement names; return updated spec and placement errors."""
    from .models import BackendSpec, DatabaseSpec, K8sWorkloadSpec

    errors: list[str] = []
    backend_workers: list[str] = []
    for raw in spec.backend.placement_workers:
        resolved = resolve_worker_node_name(raw, capacity)
        if resolved is None:
            known = ", ".join(w.name for w in capacity.worker_nodes)
            errors.append(
                f"backend.placement.workers: unknown node {raw!r} (workers: {known})"
            )
        else:
            backend_workers.append(resolved)

    db_worker: str | None = None
    db_workers: list[str] = []
    if spec.database.placement_worker:
        db_worker = resolve_worker_node_name(spec.database.placement_worker, capacity)
        if db_worker is None:
            known = ", ".join(w.name for w in capacity.worker_nodes)
            errors.append(
                f"database.placement.worker: unknown node "
                f"{spec.database.placement_worker!r} (workers: {known})"
            )
    elif spec.database.placement_workers:
        for raw in spec.database.placement_workers:
            resolved = resolve_worker_node_name(raw, capacity)
            if resolved is None:
                known = ", ".join(w.name for w in capacity.worker_nodes)
                errors.append(
                    f"database.placement.workers: unknown node {raw!r} (workers: {known})"
                )
            else:
                db_workers.append(resolved)

    if errors:
        return spec, errors

    if not backend_workers:
        backend_workers = [w.name for w in capacity.worker_nodes]

    updated = K8sWorkloadSpec(
        iteration_id=spec.iteration_id,
        namespace=spec.namespace,
        backend=BackendSpec(
            image=spec.backend.image,
            replicas=spec.backend.replicas,
            port=spec.backend.port,
            resources=spec.backend.resources,
            web_concurrency=spec.backend.web_concurrency,
            worker_class=spec.backend.worker_class,
            worker_threads=spec.backend.worker_threads,
            preload=spec.backend.preload,
            max_requests=spec.backend.max_requests,
            max_requests_jitter=spec.backend.max_requests_jitter,
            backlog=spec.backend.backlog,
            env=spec.backend.env,
            placement_workers=tuple(dict.fromkeys(backend_workers)),
            spread_replicas=spec.backend.spread_replicas,
        ),
        database=DatabaseSpec(
            enabled=spec.database.enabled,
            image=spec.database.image,
            service_name=spec.database.service_name,
            port=spec.database.port,
            replicas=spec.database.replicas,
            resources=spec.database.resources,
            primary_resources=spec.database.primary_resources,
            replica_resources=spec.database.replica_resources,
            max_connections=spec.database.max_connections,
            tuning=spec.database.tuning,
            placement_worker=db_worker,
            placement_workers=tuple(dict.fromkeys(db_workers)),
            cache=spec.database.cache,
        ),
        pooler=spec.pooler,
        read_pooler=spec.read_pooler,
        cache=spec.cache,
        labels=spec.labels,
    )
    return updated, []


def _request_cpu_m(res: ResourceSpec) -> int:
    return _parse_cpu_to_millicores(res.cpu_request)


def _request_mem_bytes(res: ResourceSpec) -> int:
    return _parse_memory_to_bytes(res.memory_request)


def _node_schedulable_budget(node: NodeCapacity) -> tuple[int, int]:
    cpu = int(node.allocatable_cpu_millicores * (1 - _NODE_RESERVE_FRACTION))
    mem = int(node.allocatable_memory_bytes * (1 - _NODE_RESERVE_FRACTION))
    return cpu, mem


def _pod_fits_node(
    cpu_req: int,
    mem_req: int,
    node: NodeCapacity,
) -> bool:
    budget_cpu, budget_mem = _node_schedulable_budget(node)
    return cpu_req <= budget_cpu and mem_req <= budget_mem


def _candidate_workers(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> tuple[NodeCapacity, ...]:
    allowed = set(spec.backend.placement_workers)
    if allowed:
        return tuple(w for w in capacity.worker_nodes if w.name in allowed)
    return capacity.worker_nodes


def _postgres_candidate_workers(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> tuple[NodeCapacity, ...]:
    if spec.database.placement_worker:
        pinned = [
            w
            for w in capacity.worker_nodes
            if w.name == spec.database.placement_worker
        ]
        return tuple(pinned)
    if spec.database.placement_workers:
        allowed = set(spec.database.placement_workers)
        return tuple(w for w in capacity.worker_nodes if w.name in allowed)
    return capacity.worker_nodes


def infer_pool_max_from_hints(app_hints: str) -> int:
    """Best-effort pool size from generated app source (for connection budgeting)."""
    import re

    if not app_hints or app_hints.startswith("("):
        return DEFAULT_APP_POOL_MAX

    # os.environ.get("PG_POOL_MAX", "10") / getenv variants
    for pattern in (
        r'(?:environ\.get|getenv)\s*\(\s*["\'](?:PG_POOL_MAX|DB_POOL_SIZE)["\']\s*,\s*["\'](\d+)["\']',
        r'(?:PG_POOL_MAX|DB_POOL_SIZE)\s*=\s*int\s*\(\s*(?:os\.)?(?:environ\.)?get(?:env)?\s*\(\s*["\'](?:PG_POOL_MAX|DB_POOL_SIZE)["\']\s*,\s*["\'](\d+)["\']',
    ):
        m = re.search(pattern, app_hints)
        if m:
            return max(1, int(m.group(1)))

    # ThreadedConnectionPool(minconn, maxconn, ...) with a numeric maxconn
    m = re.search(r"ThreadedConnectionPool\s*\(\s*\d+\s*,\s*(\d+)", app_hints)
    if m:
        return max(1, int(m.group(1)))

    # ThreadedConnectionPool(..., PG_POOL_MAX, ...) — use PG_POOL_MAX default above
    if re.search(r"ThreadedConnectionPool\s*\([^)]*(?:PG_POOL_MAX|DB_POOL_SIZE)", app_hints):
        m = re.search(
            r"(?:PG_POOL_MAX|DB_POOL_SIZE)\s*=\s*int\s*\(\s*(?:os\.)?(?:environ\.)?get(?:env)?\s*\(\s*[\"'](?:PG_POOL_MAX|DB_POOL_SIZE)[\"']\s*,\s*[\"'](\d+)[\"']",
            app_hints,
        )
        if m:
            return max(1, int(m.group(1)))

    # SQLAlchemy-style pool_size=N or pool_size = int(..., "N")
    m = re.search(r"pool_size\s*=\s*(?:int\s*\([^)]*)?[\"']?(\d+)[\"']?", app_hints)
    if m:
        return max(1, int(m.group(1)))

    m = re.search(r"max:\s*(\d+)", app_hints)
    if m:
        return max(1, int(m.group(1)))
    return DEFAULT_APP_POOL_MAX


def effective_pool_max(spec: K8sWorkloadSpec, app_hints: str) -> int:
    for key in ("DB_POOL_SIZE", "PG_POOL_MAX"):
        raw = spec.backend.env.get(key)
        if raw is not None:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
    return infer_pool_max_from_hints(app_hints)


def validate_backend_concurrency(spec: K8sWorkloadSpec) -> tuple[list[str], list[str]]:
    from .models import ALLOWED_GUNICORN_WORKER_CLASSES

    errors: list[str] = []
    warnings: list[str] = []
    wc = spec.backend.worker_class
    if wc not in ALLOWED_GUNICORN_WORKER_CLASSES:
        errors.append(
            f"backend.worker_class must be one of: "
            f"{', '.join(sorted(ALLOWED_GUNICORN_WORKER_CLASSES))}"
        )
    if spec.backend.worker_threads is not None and wc != "gthread":
        errors.append("backend.worker_threads requires backend.worker_class=gthread")
    if (
        spec.backend.max_requests is not None
        and spec.backend.max_requests_jitter is not None
        and spec.backend.max_requests_jitter > spec.backend.max_requests
    ):
        errors.append(
            "backend.max_requests_jitter cannot exceed backend.max_requests"
        )
    if wc == "gevent":
        warnings.append(
            "backend.worker_class=gevent requires gevent in the app image; "
            "use gthread unless the codebase already depends on gevent."
        )
    _, env_errors = parse_backend_env(spec.backend.env)
    errors.extend(env_errors)
    return errors, warnings


def validate_spec_against_cluster(
    spec: K8sWorkloadSpec,
    capacity: ClusterCapacity,
    *,
    app_hints: str = "",
) -> SpecValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    if spec.backend.replicas < 1:
        errors.append("backend.replicas must be >= 1")

    if spec.database.enabled and spec.database.replicas > 1:
        if spec.database.replicas > max(1, capacity.worker_count):
            warnings.append(
                f"database.replicas={spec.database.replicas} exceeds worker_count "
                f"{capacity.worker_count} — multiple DB pods may share nodes"
            )
        if spec.database.placement_worker and spec.database.replicas > 1:
            pinned = [
                w
                for w in capacity.worker_nodes
                if w.name == spec.database.placement_worker
            ]
            if pinned:
                node = pinned[0]
                cpu_b, mem_b = _node_schedulable_budget(node)
                primary_res = spec.database.effective_primary_resources()
                replica_res = spec.database.effective_replica_resources()
                read_count = max(0, spec.database.replicas - 1)
                need_cpu = _request_cpu_m(primary_res) + read_count * _request_cpu_m(
                    replica_res
                )
                need_mem = _request_mem_bytes(primary_res) + read_count * _request_mem_bytes(
                    replica_res
                )
                if need_cpu > cpu_b or need_mem > mem_b:
                    errors.append(
                        f"database.placement.worker pins all {spec.database.replicas} "
                        f"DB pods to {node.name}, but combined requests "
                        f"({need_cpu}m CPU, {need_mem} bytes mem) exceed that "
                        f"node's budget (~{cpu_b}m CPU, ~{mem_b // (2**20)}Mi mem)"
                    )

    if spec.backend.replicas > max(1, capacity.worker_count * 2):
        warnings.append(
            f"backend.replicas={spec.backend.replicas} is high for "
            f"{capacity.worker_count} workers"
        )

    if spec.backend.spread_replicas and spec.backend.replicas > capacity.worker_count:
        warnings.append(
            f"spread_replicas=true with {spec.backend.replicas} replicas on "
            f"{capacity.worker_count} workers — some nodes will run multiple backends"
        )

    pool_max = effective_pool_max(spec, app_hints)
    workers_per_pod = spec.backend.web_concurrency
    conn_per_replica = workers_per_pod * pool_max
    client_connections = spec.backend.replicas * conn_per_replica

    be_errors, be_warnings = validate_backend_concurrency(spec)
    errors.extend(be_errors)
    warnings.extend(be_warnings)

    if spec.database.enabled:
        if spec.pooler.enabled:
            pooler_errors, pooler_warnings = validate_pooler(
                spec.pooler,
                max_connections=spec.database.max_connections,
                client_connections_needed=client_connections,
            )
            errors.extend(pooler_errors)
            warnings.extend(pooler_warnings)
        if spec.read_pooler.enabled:
            if spec.database.replicas <= 1:
                errors.append(
                    "read_pooler.enabled requires database.replicas > 1 "
                    "(read pooler fronts the postgres-read Service)"
                )
            else:
                rp_errors, rp_warnings = validate_pooler(
                    spec.read_pooler,
                    max_connections=spec.database.max_connections,
                    client_connections_needed=client_connections,
                )
                errors.extend(
                    f.replace("pooler.", "read_pooler.", 1) for f in rp_errors
                )
                warnings.extend(
                    w.replace("pooler.", "read_pooler.", 1) for w in rp_warnings
                )
        else:
            needed = client_connections
            if needed > spec.database.max_connections:
                errors.append(
                    f"Connection budget exceeded: {spec.backend.replicas} replicas × "
                    f"{workers_per_pod} workers/pod × pool≤{pool_max} = {needed} "
                    f"connections, but database.max_connections="
                    f"{spec.database.max_connections}. Lower replicas, lower "
                    f"backend.web_concurrency, enable pooler, raise max_connections, "
                    f"or set backend.env.DB_POOL_SIZE smaller."
                )
        tuning_errors, tuning_warnings = validate_postgres_tuning(
            spec.database.tuning,
            memory_limit=spec.database.effective_primary_resources().memory_limit,
            max_connections=spec.database.max_connections,
        )
        errors.extend(tuning_errors)
        warnings.extend(tuning_warnings)

    cache_errors, cache_warnings = validate_cache(spec.cache)
    errors.extend(cache_errors)
    warnings.extend(cache_warnings)
    db_cache_errors, db_cache_warnings = validate_database_cache(
        spec.database.cache,
        backend_cache=spec.cache,
    )
    errors.extend(db_cache_errors)
    warnings.extend(db_cache_warnings)

    be_cpu_req = _request_cpu_m(spec.backend.resources)
    be_mem_req = _request_mem_bytes(spec.backend.resources)
    primary_res = spec.database.effective_primary_resources()
    replica_res = spec.database.effective_replica_resources()
    primary_cpu_req = _request_cpu_m(primary_res)
    primary_mem_req = _request_mem_bytes(primary_res)
    replica_cpu_req = _request_cpu_m(replica_res)
    replica_mem_req = _request_mem_bytes(replica_res)
    pooler_cpu_req = _request_cpu_m(spec.pooler.resources)
    pooler_mem_req = _request_mem_bytes(spec.pooler.resources)
    read_pooler_cpu_req = _request_cpu_m(spec.read_pooler.resources)
    read_pooler_mem_req = _request_mem_bytes(spec.read_pooler.resources)
    cache_cpu_req = _request_cpu_m(spec.cache.resources) if spec.cache.enabled else 0
    cache_mem_req = (
        _request_mem_bytes(spec.cache.resources) if spec.cache.enabled else 0
    )
    db_cache_cpu_req = 0
    db_cache_mem_req = 0
    if (
        spec.database.cache.enabled
        and not spec.database.cache.use_shared
    ):
        db_cache_cpu_req = _request_cpu_m(spec.database.cache.resources)
        db_cache_mem_req = _request_mem_bytes(spec.database.cache.resources)

    if spec.database.enabled:
        pg_nodes = _postgres_candidate_workers(spec, capacity)
        if not pg_nodes:
            if spec.database.placement_worker or spec.database.placement_workers:
                errors.append(
                    "database placement lists no schedulable worker nodes"
                )
        else:
            if not any(
                _pod_fits_node(primary_cpu_req, primary_mem_req, n) for n in pg_nodes
            ):
                smallest = min(pg_nodes, key=lambda n: n.allocatable_cpu_millicores)
                cpu_b, mem_b = _node_schedulable_budget(smallest)
                errors.append(
                    "Postgres **primary** requests do not fit on any allowed worker "
                    f"(needs {primary_cpu_req}m CPU + {primary_mem_req} bytes mem; "
                    f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
                    f"~{mem_b // (2**20)}Mi mem after {_NODE_RESERVE_FRACTION:.0%} "
                    "reserve). Shrink database.primary.resources or database.resources."
                )
            if spec.database.replicas > 1 and not any(
                _pod_fits_node(replica_cpu_req, replica_mem_req, n) for n in pg_nodes
            ):
                smallest = min(pg_nodes, key=lambda n: n.allocatable_cpu_millicores)
                cpu_b, mem_b = _node_schedulable_budget(smallest)
                errors.append(
                    "Postgres **replica** requests do not fit on any allowed worker "
                    f"(needs {replica_cpu_req}m CPU + {replica_mem_req} bytes mem; "
                    f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
                    f"~{mem_b // (2**20)}Mi mem after {_NODE_RESERVE_FRACTION:.0%} "
                    "reserve). Shrink database.replica.resources or database.resources."
                )

    if spec.database.enabled and spec.pooler.enabled:
        pooler_nodes = _candidate_workers(spec, capacity)
        if pooler_nodes and not any(
            _pod_fits_node(pooler_cpu_req, pooler_mem_req, n) for n in pooler_nodes
        ):
            smallest = min(pooler_nodes, key=lambda n: n.allocatable_cpu_millicores)
            cpu_b, mem_b = _node_schedulable_budget(smallest)
            errors.append(
                "PgBouncer pod **requests** do not fit on any allowed worker "
                f"(needs {pooler_cpu_req}m CPU + {pooler_mem_req} bytes mem; "
                f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
                f"~{mem_b // (2**20)}Mi mem). Reduce pooler.resources requests."
            )

    if spec.database.enabled and spec.read_pooler.enabled:
        rp_nodes = _candidate_workers(spec, capacity)
        if rp_nodes and not any(
            _pod_fits_node(read_pooler_cpu_req, read_pooler_mem_req, n)
            for n in rp_nodes
        ):
            smallest = min(rp_nodes, key=lambda n: n.allocatable_cpu_millicores)
            cpu_b, mem_b = _node_schedulable_budget(smallest)
            errors.append(
                "Read PgBouncer pod **requests** do not fit on any allowed worker "
                f"(needs {read_pooler_cpu_req}m CPU + {read_pooler_mem_req} bytes mem; "
                f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
                f"~{mem_b // (2**20)}Mi mem). Reduce read_pooler.resources requests."
            )

    if spec.cache.enabled:
        cache_nodes = _candidate_workers(spec, capacity)
        if cache_nodes and not any(
            _pod_fits_node(cache_cpu_req, cache_mem_req, n) for n in cache_nodes
        ):
            smallest = min(cache_nodes, key=lambda n: n.allocatable_cpu_millicores)
            cpu_b, mem_b = _node_schedulable_budget(smallest)
            errors.append(
                "Redis cache pod **requests** do not fit on any allowed worker "
                f"(needs {cache_cpu_req}m CPU + {cache_mem_req} bytes mem). "
                "Reduce cache.resources requests."
            )

    if db_cache_cpu_req > 0:
        db_cache_nodes = _candidate_workers(spec, capacity)
        if db_cache_nodes and not any(
            _pod_fits_node(db_cache_cpu_req, db_cache_mem_req, n)
            for n in db_cache_nodes
        ):
            smallest = min(
                db_cache_nodes, key=lambda n: n.allocatable_cpu_millicores
            )
            cpu_b, mem_b = _node_schedulable_budget(smallest)
            errors.append(
                "Dedicated database Redis pod **requests** do not fit on any "
                "allowed worker. Reduce database.cache.resources requests."
            )

    backend_nodes = _candidate_workers(spec, capacity)
    if not backend_nodes:
        errors.append("backend.placement.workers lists no schedulable worker nodes")
    elif not any(_pod_fits_node(be_cpu_req, be_mem_req, n) for n in backend_nodes):
        smallest = min(backend_nodes, key=lambda n: n.allocatable_cpu_millicores)
        cpu_b, mem_b = _node_schedulable_budget(smallest)
        errors.append(
            "Each backend pod **requests** must fit on at least one allowed worker "
            f"(needs {be_cpu_req}m CPU + {be_mem_req} bytes mem per pod; "
            f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
            f"~{mem_b // (2**20)}Mi mem). Reduce backend.resources requests."
        )

    read_count = max(0, spec.database.replicas - 1) if spec.database.enabled else 0
    total_cpu_req = spec.backend.replicas * be_cpu_req + (
        primary_cpu_req + read_count * replica_cpu_req if spec.database.enabled else 0
    ) + (
        pooler_cpu_req * spec.pooler.replicas
        if spec.database.enabled and spec.pooler.enabled
        else 0
    ) + (
        read_pooler_cpu_req * spec.read_pooler.replicas
        if spec.database.enabled and spec.read_pooler.enabled
        else 0
    ) + (
        cache_cpu_req * spec.cache.replicas if spec.cache.enabled else 0
    ) + (
        db_cache_cpu_req * spec.database.cache.replicas
        if db_cache_cpu_req > 0
        else 0
    )
    total_mem_req = spec.backend.replicas * be_mem_req + (
        primary_mem_req + read_count * replica_mem_req if spec.database.enabled else 0
    ) + (
        pooler_mem_req * spec.pooler.replicas
        if spec.database.enabled and spec.pooler.enabled
        else 0
    ) + (
        read_pooler_mem_req * spec.read_pooler.replicas
        if spec.database.enabled and spec.read_pooler.enabled
        else 0
    ) + (
        cache_mem_req * spec.cache.replicas if spec.cache.enabled else 0
    ) + (
        db_cache_mem_req * spec.database.cache.replicas
        if db_cache_mem_req > 0
        else 0
    )
    if total_cpu_req > capacity.budget_cpu_millicores:
        errors.append(
            f"Total CPU **requests** {total_cpu_req}m exceed cluster budget "
            f"{capacity.budget_cpu_millicores}m (after reserve)"
        )
    if total_mem_req > capacity.budget_memory_bytes:
        errors.append(
            "Total memory **requests** exceed cluster budget after reserve "
            f"({total_mem_req} bytes vs {capacity.budget_memory_bytes} bytes)"
        )

    backend_cpu_lim = spec.backend.replicas * _parse_cpu_to_millicores(
        spec.backend.resources.cpu_limit
    )
    db_cpu_lim = _parse_cpu_to_millicores(primary_res.cpu_limit) + read_count * _parse_cpu_to_millicores(
        replica_res.cpu_limit
    ) if spec.database.enabled else 0
    if backend_cpu_lim + db_cpu_lim > capacity.budget_cpu_millicores:
        warnings.append(
            f"CPU limits (~{backend_cpu_lim + db_cpu_lim}m) exceed budget "
            f"{capacity.budget_cpu_millicores}m"
        )

    return SpecValidationResult(errors=errors, warnings=warnings)

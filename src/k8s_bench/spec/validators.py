"""
Composable validators for :class:`K8sWorkloadSpec` against cluster capacity.

Each function takes ``(spec, capacity)`` and returns ``(errors, warnings)``.
:data:`SPEC_VALIDATORS` lists every rule run by
:func:`k8s_bench.spec.validate.validate_spec_against_cluster`.
"""

from __future__ import annotations

from collections.abc import Callable

from ..cluster.capacity import (
    ClusterCapacity,
    NodeCapacity,
    _parse_cpu_to_millicores,
    _parse_memory_to_bytes,
)
from .components import (
    parse_backend_env,
    validate_cache,
    validate_database_cache,
    validate_pooler,
)
from .components.postgres_tuning import validate_postgres_tuning
from .models import K8sWorkloadSpec, ResourceSpec

_NODE_RESERVE_FRACTION = 0.10

ValidatorResult = tuple[list[str], list[str]]
ValidatorFn = Callable[[K8sWorkloadSpec, ClusterCapacity], ValidatorResult]


def _request_cpu_m(res: ResourceSpec) -> int:
    return _parse_cpu_to_millicores(res.cpu_request)


def _request_mem_bytes(res: ResourceSpec) -> int:
    return _parse_memory_to_bytes(res.memory_request)


def _node_schedulable_budget(node: NodeCapacity) -> tuple[int, int]:
    cpu = int(node.allocatable_cpu_millicores * (1 - _NODE_RESERVE_FRACTION))
    mem = int(node.allocatable_memory_bytes * (1 - _NODE_RESERVE_FRACTION))
    return cpu, mem


def _pod_fits_node(cpu_req: int, mem_req: int, node: NodeCapacity) -> bool:
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


def effective_pool_max(spec: K8sWorkloadSpec) -> int | None:
    for key in ("DB_POOL_SIZE", "PG_POOL_MAX"):
        raw = spec.backend.env.get(key)
        if raw is not None:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
    return None


def estimate_app_client_connections(
    spec: K8sWorkloadSpec,
) -> tuple[int | None, int, int | None]:
    """Return ``(pool_max_per_worker, workers_per_pod, total_client_connections)``."""
    pool_max = effective_pool_max(spec)
    workers_per_pod = spec.backend.web_concurrency
    total = (
        spec.backend.replicas * workers_per_pod * pool_max
        if pool_max is not None
        else None
    )
    return pool_max, workers_per_pod, total


def validate_backend_replica_minimum(
    spec: K8sWorkloadSpec, _capacity: ClusterCapacity
) -> ValidatorResult:
    errors: list[str] = []
    if spec.backend.replicas < 1:
        errors.append("backend.replicas must be >= 1")
    return errors, []


def validate_database_topology_warnings(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    errors: list[str] = []
    warnings: list[str] = []

    if not spec.database.enabled or spec.database.replicas <= 1:
        return errors, warnings

    if spec.database.replicas > max(1, capacity.worker_count):
        warnings.append(
            f"database.replicas={spec.database.replicas} exceeds worker_count "
            f"{capacity.worker_count} — multiple DB pods may share nodes"
        )

    if spec.database.placement_worker:
        pinned = [
            w for w in capacity.worker_nodes if w.name == spec.database.placement_worker
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
    return errors, warnings


def validate_backend_scale_warnings(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    warnings: list[str] = []

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
    return [], warnings


def validate_backend_concurrency(
    spec: K8sWorkloadSpec, _capacity: ClusterCapacity
) -> ValidatorResult:
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


def validate_database_connections(
    spec: K8sWorkloadSpec, _capacity: ClusterCapacity
) -> ValidatorResult:
    if not spec.database.enabled:
        return [], []

    errors: list[str] = []
    warnings: list[str] = []
    pool_max, workers_per_pod, client_connections = estimate_app_client_connections(spec)
    max_conn = spec.database.max_connections

    if pool_max is None:
        warnings.append(
            "backend.env.DB_POOL_SIZE / PG_POOL_MAX not set; skipping app→DB connection "
            "budget checks."
        )

    if spec.pooler.enabled:
        pooler_errors, pooler_warnings = validate_pooler(
            spec.pooler,
            max_connections=max_conn,
            client_connections_needed=(client_connections or 0),
        )
        errors.extend(pooler_errors)
        warnings.extend(pooler_warnings)
        if client_connections is None:
            warnings.append(
                "pooler.max_client_conn was not validated because DB_POOL_SIZE / PG_POOL_MAX "
                "was not set in the spec."
            )
    elif client_connections is not None and client_connections > max_conn:
        errors.append(
            f"Direct Postgres connection budget exceeded: "
            f"{spec.backend.replicas} replicas × {workers_per_pod} workers/pod × "
            f"pool≤{pool_max} = {client_connections} client connections, but "
            f"database.max_connections={max_conn}. Lower replicas, lower "
            f"backend.web_concurrency, enable pooler, raise max_connections, "
            f"or set backend.env.DB_POOL_SIZE smaller."
        )

    if spec.read_pooler.enabled:
        if spec.database.replicas <= 1:
            errors.append(
                "read_pooler.enabled requires database.replicas > 1 "
                "(read pooler fronts the postgres-read Service)"
            )
        else:
            rp_errors, rp_warnings = validate_pooler(
                spec.read_pooler,
                max_connections=max_conn,
                client_connections_needed=(client_connections or 0),
            )
            errors.extend(f.replace("pooler.", "read_pooler.", 1) for f in rp_errors)
            warnings.extend(w.replace("pooler.", "read_pooler.", 1) for w in rp_warnings)
            if client_connections is None:
                warnings.append(
                    "read_pooler.max_client_conn was not validated because DB_POOL_SIZE / "
                    "PG_POOL_MAX was not set in the spec."
                )

    return errors, warnings


def validate_database_postgres_tuning(
    spec: K8sWorkloadSpec, _capacity: ClusterCapacity
) -> ValidatorResult:
    if not spec.database.enabled:
        return [], []
    return validate_postgres_tuning(
        spec.database.tuning,
        memory_limit=spec.database.effective_primary_resources().memory_limit,
        max_connections=spec.database.max_connections,
    )


def validate_application_cache(
    spec: K8sWorkloadSpec, _capacity: ClusterCapacity
) -> ValidatorResult:
    return validate_cache(spec.cache)


def validate_database_cache_config(
    spec: K8sWorkloadSpec, _capacity: ClusterCapacity
) -> ValidatorResult:
    return validate_database_cache(
        spec.database.cache,
        backend_cache=spec.cache,
    )


def validate_postgres_pod_fit(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    if not spec.database.enabled:
        return [], []

    errors: list[str] = []
    primary_res = spec.database.effective_primary_resources()
    replica_res = spec.database.effective_replica_resources()
    primary_cpu_req = _request_cpu_m(primary_res)
    primary_mem_req = _request_mem_bytes(primary_res)
    replica_cpu_req = _request_cpu_m(replica_res)
    replica_mem_req = _request_mem_bytes(replica_res)

    pg_nodes = _postgres_candidate_workers(spec, capacity)
    if not pg_nodes:
        if spec.database.placement_worker or spec.database.placement_workers:
            errors.append("database placement lists no schedulable worker nodes")
        return errors, []

    if not any(_pod_fits_node(primary_cpu_req, primary_mem_req, n) for n in pg_nodes):
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

    return errors, []


def validate_write_pooler_pod_fit(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    if not spec.database.enabled or not spec.pooler.enabled:
        return [], []

    errors: list[str] = []
    pooler_cpu_req = _request_cpu_m(spec.pooler.resources)
    pooler_mem_req = _request_mem_bytes(spec.pooler.resources)
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
    return errors, []


def validate_read_pooler_pod_fit(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    if not spec.database.enabled or not spec.read_pooler.enabled:
        return [], []

    errors: list[str] = []
    read_pooler_cpu_req = _request_cpu_m(spec.read_pooler.resources)
    read_pooler_mem_req = _request_mem_bytes(spec.read_pooler.resources)
    rp_nodes = _candidate_workers(spec, capacity)
    if rp_nodes and not any(
        _pod_fits_node(read_pooler_cpu_req, read_pooler_mem_req, n) for n in rp_nodes
    ):
        smallest = min(rp_nodes, key=lambda n: n.allocatable_cpu_millicores)
        cpu_b, mem_b = _node_schedulable_budget(smallest)
        errors.append(
            "Read PgBouncer pod **requests** do not fit on any allowed worker "
            f"(needs {read_pooler_cpu_req}m CPU + {read_pooler_mem_req} bytes mem; "
            f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
            f"~{mem_b // (2**20)}Mi mem). Reduce read_pooler.resources requests."
        )
    return errors, []


def validate_application_cache_pod_fit(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    if not spec.cache.enabled:
        return [], []

    errors: list[str] = []
    cache_cpu_req = _request_cpu_m(spec.cache.resources)
    cache_mem_req = _request_mem_bytes(spec.cache.resources)
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
    return errors, []


def validate_dedicated_db_cache_pod_fit(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    if not (spec.database.cache.enabled and not spec.database.cache.use_shared):
        return [], []

    errors: list[str] = []
    db_cache_cpu_req = _request_cpu_m(spec.database.cache.resources)
    db_cache_mem_req = _request_mem_bytes(spec.database.cache.resources)
    db_cache_nodes = _candidate_workers(spec, capacity)
    if db_cache_nodes and not any(
        _pod_fits_node(db_cache_cpu_req, db_cache_mem_req, n) for n in db_cache_nodes
    ):
        smallest = min(db_cache_nodes, key=lambda n: n.allocatable_cpu_millicores)
        cpu_b, mem_b = _node_schedulable_budget(smallest)
        errors.append(
            "Dedicated database Redis pod **requests** do not fit on any "
            "allowed worker. Reduce database.cache.resources requests."
        )
    return errors, []


def validate_backend_pod_fit(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    errors: list[str] = []
    be_cpu_req = _request_cpu_m(spec.backend.resources)
    be_mem_req = _request_mem_bytes(spec.backend.resources)
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
    return errors, []


def validate_cluster_resource_requests_budget(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    errors: list[str] = []

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
    if spec.database.cache.enabled and not spec.database.cache.use_shared:
        db_cache_cpu_req = _request_cpu_m(spec.database.cache.resources)
        db_cache_mem_req = _request_mem_bytes(spec.database.cache.resources)

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
        db_cache_cpu_req * spec.database.cache.replicas if db_cache_cpu_req > 0 else 0
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
        db_cache_mem_req * spec.database.cache.replicas if db_cache_mem_req > 0 else 0
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
    return errors, []


def validate_cpu_limits_vs_budget_warning(
    spec: K8sWorkloadSpec, capacity: ClusterCapacity
) -> ValidatorResult:
    warnings: list[str] = []
    primary_res = spec.database.effective_primary_resources()
    replica_res = spec.database.effective_replica_resources()
    read_count = max(0, spec.database.replicas - 1) if spec.database.enabled else 0

    backend_cpu_lim = spec.backend.replicas * _parse_cpu_to_millicores(
        spec.backend.resources.cpu_limit
    )
    db_cpu_lim = (
        _parse_cpu_to_millicores(primary_res.cpu_limit)
        + read_count * _parse_cpu_to_millicores(replica_res.cpu_limit)
        if spec.database.enabled
        else 0
    )
    if backend_cpu_lim + db_cpu_lim > capacity.budget_cpu_millicores:
        warnings.append(
            f"CPU limits (~{backend_cpu_lim + db_cpu_lim}m) exceed budget "
            f"{capacity.budget_cpu_millicores}m"
        )
    return [], warnings


SPEC_VALIDATORS: tuple[ValidatorFn, ...] = (
    validate_backend_replica_minimum,
    validate_database_topology_warnings,
    validate_backend_scale_warnings,
    validate_backend_concurrency,
    validate_database_connections,
    validate_database_postgres_tuning,
    validate_application_cache,
    validate_database_cache_config,
    validate_postgres_pod_fit,
    validate_write_pooler_pod_fit,
    validate_read_pooler_pod_fit,
    validate_application_cache_pod_fit,
    validate_dedicated_db_cache_pod_fit,
    validate_backend_pod_fit,
    validate_cluster_resource_requests_budget,
    validate_cpu_limits_vs_budget_warning,
)

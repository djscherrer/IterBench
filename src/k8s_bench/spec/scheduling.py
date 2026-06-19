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
            max_connections=spec.database.max_connections,
            tuning=spec.database.tuning,
            placement_worker=db_worker,
            placement_workers=tuple(dict.fromkeys(db_workers)),
        ),
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
    import re

    m = re.search(r"max:\s*(\d+)", app_hints)
    if m:
        return max(1, int(m.group(1)))
    return DEFAULT_APP_POOL_MAX


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
                need_cpu = spec.database.replicas * _request_cpu_m(
                    spec.database.resources
                )
                need_mem = spec.database.replicas * _request_mem_bytes(
                    spec.database.resources
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

    pool_max = infer_pool_max_from_hints(app_hints)
    workers_per_pod = spec.backend.web_concurrency
    # Each worker process keeps its own connection pool, so a single replica can
    # open up to web_concurrency × pool_max connections.
    conn_per_replica = workers_per_pod * pool_max
    if spec.database.enabled:
        needed = spec.backend.replicas * conn_per_replica
        if needed > spec.database.max_connections:
            errors.append(
                f"Connection budget exceeded: {spec.backend.replicas} replicas × "
                f"{workers_per_pod} workers/pod × pool≤{pool_max} = {needed} "
                f"connections, but database.max_connections="
                f"{spec.database.max_connections}. Lower replicas, lower "
                f"backend.web_concurrency, raise max_connections, or shrink the "
                f"app connection pool."
            )
        tuning_errors, tuning_warnings = validate_postgres_tuning(
            spec.database.tuning,
            memory_limit=spec.database.resources.memory_limit,
            max_connections=spec.database.max_connections,
        )
        errors.extend(tuning_errors)
        warnings.extend(tuning_warnings)

    be_cpu_req = _request_cpu_m(spec.backend.resources)
    be_mem_req = _request_mem_bytes(spec.backend.resources)
    db_cpu_req = _request_cpu_m(spec.database.resources)
    db_mem_req = _request_mem_bytes(spec.database.resources)

    if spec.database.enabled:
        pg_nodes = _postgres_candidate_workers(spec, capacity)
        if not pg_nodes:
            if spec.database.placement_worker or spec.database.placement_workers:
                errors.append(
                    "database placement lists no schedulable worker nodes"
                )
        elif not any(_pod_fits_node(db_cpu_req, db_mem_req, n) for n in pg_nodes):
            smallest = min(pg_nodes, key=lambda n: n.allocatable_cpu_millicores)
            cpu_b, mem_b = _node_schedulable_budget(smallest)
            errors.append(
                "Postgres **requests** do not fit on any allowed worker alone "
                f"(needs {db_cpu_req}m CPU + {db_mem_req} bytes mem requests; "
                f"example worker {smallest.name} budget ~{cpu_b}m CPU / "
                f"~{mem_b // (2**20)}Mi mem after {_NODE_RESERVE_FRACTION:.0%} reserve). "
                "A single pod cannot span machines — shrink database.resources requests."
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

    total_cpu_req = spec.backend.replicas * be_cpu_req + (
        db_cpu_req * spec.database.replicas if spec.database.enabled else 0
    )
    total_mem_req = spec.backend.replicas * be_mem_req + (
        db_mem_req * spec.database.replicas if spec.database.enabled else 0
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
    db_cpu_lim = _parse_cpu_to_millicores(spec.database.resources.cpu_limit) * (
        spec.database.replicas if spec.database.enabled else 0
    )
    if backend_cpu_lim + db_cpu_lim > capacity.budget_cpu_millicores:
        warnings.append(
            f"CPU limits (~{backend_cpu_lim + db_cpu_lim}m) exceed budget "
            f"{capacity.budget_cpu_millicores}m"
        )

    return SpecValidationResult(errors=errors, warnings=warnings)

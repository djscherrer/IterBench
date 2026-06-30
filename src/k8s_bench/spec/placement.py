"""
Resolve and canonicalize workload spec placement against cluster worker nodes.

Runs after parsing the LLM ``<SPEC>`` fragment and before
:func:`k8s_bench.spec.validate.validate_spec_against_cluster`.
"""

from __future__ import annotations

from ..cluster.capacity import ClusterCapacity
from .models import BackendSpec, DatabaseSpec, K8sWorkloadSpec


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

"""K8s deployment context block for code-refinement prompts."""

from __future__ import annotations

from pathlib import Path

from ..spec.models import K8sWorkloadSpec
from ..spec.scheduling import effective_pool_max
from ..workspace import find_iteration_spec_path, latest_spec_path


def resolve_active_spec(
    iteration_path: Path, sample_dir: Path
) -> tuple[K8sWorkloadSpec, Path, Path] | None:
    own = find_iteration_spec_path(iteration_path)
    if own is not None:
        try:
            return K8sWorkloadSpec.from_yaml_file(own), own, iteration_path
        except Exception:
            pass

    latest = latest_spec_path(sample_dir)
    if latest is None:
        return None
    spec_path, source_dir = latest
    try:
        return K8sWorkloadSpec.from_yaml_file(spec_path), spec_path, source_dir
    except Exception:
        return None


def format_k8s_deployment_context(
    iteration_path: Path, sample_dir: Path
) -> str:
    resolved = resolve_active_spec(iteration_path, sample_dir)
    if resolved is None:
        return ""
    spec, _spec_path, source_dir = resolved
    backend = spec.backend
    db = spec.database
    pooler = spec.pooler
    read_pooler = spec.read_pooler
    cache = spec.cache

    pool_max = effective_pool_max(spec, "")

    worker_line = (
        f"- **Gunicorn workers**: {backend.web_concurrency} "
        f"(`{backend.worker_class}`"
        + (
            f", {backend.worker_threads} threads/process"
            if backend.worker_class == "gthread" and backend.worker_threads
            else ""
        )
        + ")"
    )

    lines: list[str] = [
        "### K8s deployment context (read-only here — set by the spec stage)",
        "",
        (
            "The application will run under the deployment below. **Treat this "
            "as a binding constraint when sizing per-pod resources in code** "
            "(notably DB connection pool size, worker counts, in-memory caches). "
            "Don't reshape the deployment — change the code to fit it."
        ),
        "",
        f"- **Source spec**: `{source_dir.name}` (most recent on disk; "
        "may be from a failed iteration if benchmark didn't run)",
        f"- **Backend replicas**: {backend.replicas}",
        worker_line,
        f"- **Backend resources**: cpu {backend.resources.cpu_request}/"
        f"{backend.resources.cpu_limit}, "
        f"mem {backend.resources.memory_request}/{backend.resources.memory_limit}",
    ]

    if pooler.enabled:
        lines.append(
            f"- **Write pooler**: PgBouncer `{pooler.service_name}` "
            f"({pooler.mode}, {pooler.replicas} replica(s), "
            f"pool={pooler.default_pool_size}, max_clients={pooler.max_client_conn}) "
            "→ `DB_HOST`"
        )
    else:
        lines.append("- **Write pooler**: disabled (`DB_HOST` → Postgres primary)")

    if read_pooler.enabled:
        lines.append(
            f"- **Read pooler**: PgBouncer `{read_pooler.service_name}` "
            f"({read_pooler.mode}, {read_pooler.replicas} replica(s), "
            f"pool={read_pooler.default_pool_size}) → `DB_READ_HOST`"
        )
    elif db.enabled and db.replicas > 1:
        lines.append("- **Read pooler**: disabled (`DB_READ_HOST` → `postgres-read` Service)")

    if cache.enabled:
        lines.append(
            f"- **App Redis**: enabled (`REDIS_URL` → `{cache.service_name}`) — "
            "only helps if code uses it"
        )
    if db.cache.enabled:
        shared = "shared with app Redis" if db.cache.use_shared else "dedicated redis-db"
        lines.append(f"- **DB Redis**: enabled (`DB_REDIS_URL`, {shared})")

    if db.enabled:
        topology = (
            f"1 primary + {db.replicas - 1} read replica(s) (streaming replication)"
            if db.replicas > 1
            else "single primary"
        )
        primary = db.effective_primary_resources()
        replica = db.effective_replica_resources()
        lines.extend(
            [
                f"- **Postgres replicas**: {db.replicas} ({topology})",
                f"- **Postgres `max_connections`**: {db.max_connections}",
                f"- **Postgres default resources**: cpu {db.resources.cpu_request}/"
                f"{db.resources.cpu_limit}, "
                f"mem {db.resources.memory_request}/{db.resources.memory_limit}",
            ]
        )
        if db.primary_resources is not None:
            lines.append(
                f"- **Postgres primary override**: cpu {primary.cpu_request}/"
                f"{primary.cpu_limit}, mem {primary.memory_request}/"
                f"{primary.memory_limit}"
            )
        if db.replicas > 1 and db.replica_resources is not None:
            lines.append(
                f"- **Postgres replica override**: cpu {replica.cpu_request}/"
                f"{replica.cpu_limit}, mem {replica.memory_request}/"
                f"{replica.memory_limit}"
            )
    else:
        lines.append("- **Database**: disabled")

    if backend.env:
        env_entries = ", ".join(
            f"`{k}={v}`" for k, v in sorted(backend.env.items())
        )
        lines.append(f"- **Backend env (spec)**: {env_entries}")
    else:
        lines.append("- **Backend env (spec)**: (none)")

    if db.enabled:
        client_conns = backend.replicas * backend.web_concurrency * pool_max
        lines.extend(
            [
                "",
                (
                    f"**Connection budget**: ~{client_conns} app-side client connections "
                    f"({backend.replicas} pods × {backend.web_concurrency} workers × "
                    f"pool≤{pool_max}). With `gthread`, threads share one pool per worker — "
                    "size `PG_POOL_MAX` / `DB_POOL_SIZE` to match in-flight work, not thread count."
                ),
            ]
        )
        if pooler.enabled:
            lines.append(
                f"- Write path multiplexed through PgBouncer "
                f"(server pool ≤ {pooler.default_pool_size})."
            )
        elif db.max_connections:
            per_pod = max((db.max_connections - 10) // max(backend.replicas, 1), 1)
            lines.append(
                f"- Without pooler, target **≤ {per_pod}** pool connections per pod "
                f"({db.max_connections} `max_connections` on primary)."
            )
        if db.replicas > 1:
            lines.append(
                "- `DB_READ_HOST` / `DB_READ_PORT` are set — route read-only queries "
                "(GET/list/export) through the read pool; keep writes on `DB_HOST`."
            )

    return "\n".join(lines)

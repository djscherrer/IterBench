"""
LLM-driven generation of ``iterations/NNN/spec/spec.yaml`` deployment parameters.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
from pathlib import Path
from typing import Any

import yaml

from env.base import Env
from llm import Prompter
from scenarios.base import Scenario

from ..feedback import IterationFeedback
from ..cluster.capacity import (
    ClusterCapacity,
    capacity_as_json,
    collect_cluster_capacity,
)
from .models import (
    BackendSpec,
    DatabaseSpec,
    K8sWorkloadSpec,
    ResourceSpec,
)
from .pooler import DEFAULT_READ_POOLER_SERVICE, PoolerSpec
from .cache import CacheSpec
from .scheduling import (
    SpecValidationError,
    infer_pool_max_from_hints,
    normalize_spec_placement,
    validate_spec_against_cluster,
)
from ..workspace import (
    PROMPT_LOG_FILENAME,
    RESPONSE_LOG_FILENAME,
    attempt_subdir,
    default_k8s_namespace,
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    iteration_spec_attempts_dir,
    iteration_spec_dir,
    iteration_spec_path,
    latest_code_dir,
    new_iteration_id,
    next_attempt_index,
    normalize_iteration_id,
    resolve_iteration_dir,
)
from .render import render_iteration

_IMAGE_PLACEHOLDER = "baxbench/pending-at-bench:latest"

def _benchmark_load_hint(scenario: Scenario) -> str:
    """Scenario-specific load description (replaces stale generic endpoint examples)."""
    return (
        f"The deployment will be exercised by adaptive Locust load testing on scenario "
        f"**{scenario.id}** (sustained mixed HTTP traffic against the API). Size replicas "
        "and resources to maximize **goodput** (sustained rate of **successful** HTTP "
        "responses) under that pressure. Raw throughput that comes with elevated error "
        "rates is NOT a win — failed requests do not count."
    )


def _format_iteration_progress(
    *, iteration_index: int, total_iterations: int
) -> str:
    """Human-friendly progress line, e.g. ``Iteration 4 of 10 (refinement)``."""
    if total_iterations <= 0:
        return f"Iteration {iteration_index}"
    remaining = max(0, total_iterations - iteration_index - 1)
    kind = "baseline" if iteration_index == 0 else "refinement"
    return (
        f"Iteration {iteration_index} of {total_iterations - 1} ({kind}); "
        f"{remaining} more iteration(s) remain after this one."
    )

_SPEC_BLOCK_RE = re.compile(r"<SPEC>\s*(.*?)\s*</SPEC>", re.DOTALL | re.IGNORECASE)
_YAML_FENCE_RE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _safety_performance_text(
    env: Env, scenario: Scenario, safety_prompt: str
) -> str:
    """Reuse the same performance instruction as application code generation."""
    prompt = scenario.build_prompt(
        env,
        spec_type="openapi",
        safety_prompt=safety_prompt,
        agent=False,
        use_stubs=False,
    )
    for line in prompt.splitlines():
        if "high-workload" in line or "thousands of requests" in line:
            return line.strip()
        if "concurrent users" in line and "performance" in line:
            return line.strip()
    return (
        "Optimize for very high concurrent load and sustainable throughput under benchmark."
    )


def _read_app_hints(code_dir: pathlib.Path, *, max_chars: int = 4000) -> str:
    if not code_dir.is_dir():
        return "(application code not found yet)"
    candidates = ["app.js", "main.py", "app.py", "server.js", "index.js"]
    for name in candidates:
        path = code_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            return f"Excerpt from {name}:\n{text}"
    files = sorted(code_dir.glob("*"))[:5]
    if not files:
        return "(empty code directory)"
    return "Code files: " + ", ".join(p.name for p in files)


def build_k8s_spec_prompt(
    *,
    env: Env,
    scenario: Scenario,
    safety_prompt: str,
    capacity: ClusterCapacity,
    app_hints: str,
    iteration_id: str,
    iteration_index: int = 0,
    total_iterations: int = 0,
    prior_feedback: IterationFeedback | str | None = None,
    validation_feedback: str | None = None,
) -> str:
    perf = _safety_performance_text(env, scenario, safety_prompt)
    pool_max = infer_pool_max_from_hints(app_hints)
    progress = _format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    if prior_feedback is not None:
        fb_text = (
            prior_feedback.to_prompt_text()
            if isinstance(prior_feedback, IterationFeedback)
            else str(prior_feedback).strip()
        )
        goal = f"""## Goal
You are refining deployment parameters for iteration `{iteration_id}` after a benchmark of the **previous** iteration.

**Progress**: {progress} Plan your remaining budget — bold experiments early, consolidate refinements toward the end.

Use the feedback below to tune **all** deployment levers: backend replicas/concurrency, pooler/read_pooler, optional Redis cache, Postgres replicas/resources/GUCs, and placement. Reduce errors and saturation; **maximize goodput (sustained rate of successful responses)**. Raw throughput with high error rates does NOT count. If feedback shows **overload / high fail%** at modest user counts, prefer simplifying the stack (fewer hops, symmetric DB sizing) before adding more tiers.

{perf}

## Feedback from previous benchmark
{fb_text}
"""
    else:
        goal = f"""## Goal
Propose deployment parameters for iteration `{iteration_id}` so the application can sustain **very high concurrent user load** in a Locust benchmark while **maximizing goodput (successful responses per second)**. Failed requests do not count toward your score.

**Progress**: {progress}

{perf}
"""
    validation_block = ""
    if validation_feedback:
        validation_block = f"""
{validation_feedback.strip()}

"""
    worker_lines = []
    for w in capacity.worker_nodes:
        worker_lines.append(
            f"- `{w.name}`: allocatable **{w.allocatable_cpu_millicores}m CPU** / "
            f"**{round(w.allocatable_memory_bytes / (2**30), 2)} Gi** memory "
            f"(schedulable; leave ~10% headroom per node)"
        )
    workers_text = "\n".join(worker_lines) if worker_lines else "(no worker nodes reported)"

    return f"""You are a Kubernetes deployment tuning expert for BaxBench performance experiments.

{goal}{validation_block}## Application
- Scenario: {scenario.id}
- Environment: {env.id} (listen port {env.port})
- Database required: {scenario.needs_db}
- App DB connection pool (per worker process): **{pool_max}** connections (inferred from generated code, unless you set `backend.env.PG_POOL_MAX` or `backend.env.DB_POOL_SIZE`)
- A replica runs `backend.web_concurrency` worker processes; with `worker_class=gthread`, each process also runs `worker_threads` concurrent requests **but shares one DB pool per process** — effective DB concurrency per pod ≈ `web_concurrency × pool_max`, not `web_concurrency × worker_threads`

{app_hints}

## Cluster capacity
Schedulable **workers only** (control-plane excluded). Use **requests** for scheduling fit.

**Cluster budget (sum across workers, after {capacity.suggested_reserve_fraction:.0%} reserve):**
- CPU: {capacity.budget_cpu_millicores}m (~{capacity.budget_cpu_millicores / 1000:.1f} cores)
- Memory: ~{round(capacity.budget_memory_bytes / (2**30), 2)} Gi

**Per-worker capacity (each pod must fit on ONE of these nodes):**
{workers_text}

```json
{capacity_as_json(capacity)}
```

## Scheduling rules (critical — hard limits enforced before deploy)
1. **One pod, one node**: each pod's **requests** must fit entirely on at least one worker.
2. **Connection budget (no pooler)**: `backend.replicas × backend.web_concurrency × pool_max ≤ database.max_connections` on the **primary** (pool_max from code or `backend.env.PG_POOL_MAX` / `backend.env.DB_POOL_SIZE`).
3. **Connection budget (pooler enabled)**: the same product must be ≤ `pooler.max_client_conn` for **writes**; if `read_pooler` is enabled, the same product must also fit `read_pooler.max_client_conn` for **reads**. PgBouncer opens ≤ `default_pool_size` server connections per pooler tier (each must be ≤ `database.max_connections`).
4. **Cluster budget**: sum of all pod requests (backends + primary + read replicas + `pooler.replicas` + `read_pooler.replicas` (if enabled) + `cache.replicas` + dedicated `database.cache` Redis if `use_shared: false`) must fit cluster capacity after reserve.
5. Optional **placement**: restrict or pin which workers may run postgres/backends; `spread_replicas: true` spreads backend pods across nodes.
6. Use worker **`name` values** from the per-worker list (short names like `node3` are accepted).

## Optimization objective
**Maximize goodput** — successful HTTP responses per second sustained over the run. Failed requests (5xx, timeouts, connection errors) are **not counted** as wins. A configuration that processes 200 req/s with 0% errors beats one that processes 500 req/s with 20% errors.

## Spec fields (semantics — you choose values)
Use **benchmark feedback** from prior iterations to refine replicas and resources. The framework validates feasibility; it does not prescribe tuning targets.

**`backend`** (horizontally scalable — many stateless pods):
- `replicas`: pod count behind the Service
- `web_concurrency`: gunicorn/PM2 **processes** per pod (`--workers`). More processes = more parallelism but more DB client connections (`web_concurrency × pool_max` per replica unless pooler multiplexes).
- `worker_class`: gunicorn worker type — `sync` (default, one request per process), `gthread` (threads per process; pair with `worker_threads`), or `gevent` (requires gevent in the image).
- `worker_threads`: threads per process when `worker_class=gthread` (e.g. `4` processes × `8` threads = 32 in-flight requests per pod with fewer DB pools than 32 sync workers).
- `preload`: gunicorn `--preload` (default `true`) — loads app once before forking workers (faster startup, shared memory); set `false` to isolate workers after code/config changes.
- `max_requests` / `max_requests_jitter`: recycle gunicorn workers after N requests (± jitter) to curb memory leaks under sustained load.
- `backlog`: socket listen backlog for burst connection acceptance.
- `env`: optional **allow-listed** tuning knobs (see below). Framework sets `DB_*`, `PORT`, `WEB_CONCURRENCY`, `REDIS_URL`, `DB_REDIS_URL`; do not duplicate those.
- `resources`: per-pod CPU/memory requests & limits (scheduling uses **requests**)
- `placement.workers`: optional node allow-list (omit = any worker)
- `placement.spread_replicas`: prefer spreading pods across nodes (default true)

**`pooler`** (PgBouncer — optional connection multiplexer in front of Postgres **primary**):
- Sits between app pods and the primary. Framework sets `DB_HOST`/`DB_PORT` to the pooler when enabled.
- `enabled`: `true` to deploy PgBouncer (recommended when scaling `replicas` or `web_concurrency` risks exhausting `max_connections`).
- `mode`:
  - `transaction` (**recommended**): multiplexes many short client connections onto a small server pool; best for typical REST handlers that commit per request.
  - `session`: one server connection per client session; use only if the app needs session-pinned features (prepared statements, temp tables, `LISTEN`).
- `max_client_conn`: max incoming connections from all app pods combined (write path).
- `default_pool_size`: max server connections PgBouncer opens to Postgres per user/database (keep ≤ `database.max_connections`).
- `replicas`: PgBouncer pod count behind the pooler Service (scale when the pooler pod becomes CPU-bound).
- `min_pool_size`: minimum server connections kept open per user/database pool.
- `reserve_pool_size`: extra server connections opened under burst load before queuing.
- `resources`: CPU/memory for pooler pods (scale when diagnostics show pooler CPU high or `cl_waiting` > 0).
- `service_name`: Kubernetes Service name (default `pgbouncer`; do not reuse for read pooler).

**`read_pooler`** (optional PgBouncer in front of **`postgres-read`** — requires `database.replicas > 1`):
- Same fields as `pooler`. When enabled, framework sets `DB_READ_HOST`/`DB_READ_PORT` to the read pooler Service instead of direct replica access.
- **Not a cache** — only pools/routes read connections. Use when many app workers open read connections and replicas risk connection exhaustion.
- `service_name`: must differ from write pooler (default `pgbouncer-read`). Never set both poolers to the same name.
- Add complexity only when the app already uses `DB_READ_HOST` for read-only queries; otherwise replicas stay idle.

**`cache`** (optional Redis for **application-level** caching):
- `enabled`: deploy Redis and expose `REDIS_URL` to backends. **Only helps goodput if application code uses Redis** (response cache, hot keys, etc.).
- `replicas`, `maxmemory`, `maxmemory_policy`, `resources`: scale and size the Redis pod(s).

**`database`** (Postgres):
- `replicas`: `1` = single standalone pod; `N>1` = **1 primary + (N−1) streaming read replicas** (async WAL replication; standard K8s pattern). Writes go to the primary via the `postgres` Service (`DB_HOST` env var). When `N>1`, the framework also exposes `DB_READ_HOST` (the `postgres-read` Service or read pooler). The **application code** decides whether to use it. **Bumping replicas only improves goodput if the code uses `DB_READ_HOST` for read-only queries** — otherwise replicas sit idle while the primary remains the bottleneck. If the current code only references `DB_HOST` (single pool), keep `replicas: 1` until a code refinement adds a read pool.
- `max_connections`: primary connection limit (`max_connections` on primary only)
- `cache` (optional **database-adjacent** Redis):
  - `enabled`: expose `DB_REDIS_URL` for query-result / aggregate caching in app code.
  - `use_shared: true` (default): reuse the root `cache` Redis (`REDIS_URL` and `DB_REDIS_URL` point at the same Service; requires `cache.enabled: true`).
  - `use_shared: false`: deploy dedicated `redis-db` with its own resources.
- `tuning`: optional Postgres performance settings applied to **every** database pod (primary + replicas). Use Kubernetes memory quantities (`256Mi`, `1Gi`) for memory GUCs.
  - `shared_buffers`: in-memory page cache (often ~25% of primary pod memory limit; must not exceed `database.primary.resources.memory_limit` or `database.resources.memory_limit`)
  - `effective_cache_size`: planner hint for OS + PG cache (can be ~50–75% of pod memory)
  - `work_mem`: per-sort/hash memory per operation (keep modest under high `max_connections`)
  - `maintenance_work_mem`: memory for VACUUM, CREATE INDEX, etc.
  - `max_parallel_workers_per_gather`: parallel workers per query gather node (0 disables parallel scans for that query)
  - `max_parallel_workers`: cluster-wide cap on parallel worker processes (must be ≥ `max_parallel_workers_per_gather`)
  - `max_worker_processes`: background worker cap (autovacuum, parallel workers); must be ≥ `max_parallel_workers`
  - `random_page_cost`: planner cost of non-sequential page fetch (lower on SSD/NVMe, e.g. `1.1`)
  - `effective_io_concurrency`: concurrent I/O operations the planner assumes (higher on SSD, e.g. `200`)
  - `max_wal_size`: WAL volume before forced checkpoint (write-heavy workloads)
  - `checkpoint_timeout`: seconds between checkpoints (smoother write I/O)
  - `wal_buffers`: WAL buffer memory
  - `jit_enabled: false`: disable JIT for short OLTP queries (often faster)
  - `statement_timeout_ms`: kill queries running longer than N ms (protects pools under load; avoid low values if the app runs DDL on startup against the primary — can cause 500s during warmup)
- `resources`: default per **database pod** when `primary` / `replica` overrides are omitted
- `primary.resources`: optional override for the **write primary** only (use to add CPU/memory to the saturated write path)
- `replica.resources`: optional override for **read replica** pods (often smaller than primary)
- `placement.worker`: pin all DB pods to one node (only if combined requests fit that node)
- `placement.workers`: allow-list of nodes for DB pods

When `database.replicas > 1`, the framework renders a replication-aware Postgres image (Bitnami) with primary Deployment + replica StatefulSet. This mirrors production (primary/replica), not multi-master active-active.

## Tuning heuristics (from benchmark feedback)
- **Low p95 but low goodput** often means the adaptive ramp stopped on **fail% overload**, not a clean plateau — fix errors or saturation before chasing latency.
- **Low backend CPU + low goodput** → software/path bottleneck (pool size, pooler queue, missing read routing), not “add more backend replicas”.
- **Primary saturated, replicas idle** → app must use `DB_READ_HOST` for reads before adding `read_pooler` or more replicas.
- **PgBouncer `cl_waiting` > 0** in diagnostics → raise `default_pool_size`, `max_client_conn`, or pooler `replicas`/`resources`.
- Prefer **one structural change per iteration** when exploring pooler + read pooler + asymmetric DB — easier to attribute goodput deltas.

**`backend.env` allow-list** (optional; values are strings):
- `PG_POOL_MAX`: max connections per worker process pool (psycopg2/raw SQL apps; preferred when code reads this env var).
- `DB_POOL_SIZE`: same role for SQLAlchemy / alternate naming in generated code.
- `DB_POOL_OVERFLOW`: extra burst connections per pool (if the app supports it).
- `GUNICORN_TIMEOUT`: worker timeout seconds (passed to gunicorn `--timeout`).
- `GUNICORN_KEEPALIVE`: HTTP keep-alive seconds (`--keep-alive`).
- `SQLALCHEMY_POOL_RECYCLE`: pool recycle interval for SQLAlchemy-based apps.

## Rules
1. Output **only** a YAML fragment for `backend`, optional `pooler`, optional `read_pooler`, optional `cache`, and `database` (no manifests, no namespace).
2. Set `backend.replicas` (integer >= 1).
3. Set `backend.resources` and `database.resources` with valid Kubernetes quantities (`500m`, `1`, `512Mi`, `2Gi`).
4. Keep **sum of requests** within cluster budget (include all pooler, cache, and DB pods; count 1 primary + (replicas−1) read-replica pods separately if using `database.primary` / `database.replica` overrides).
5. Do **not** set `image`, `port`, `namespace`, or framework-managed env (`DB_HOST`, `PORT`, `REDIS_URL`, etc.).

## Benchmark load
{_benchmark_load_hint(scenario)}

## Output format
Return exactly one block:

<SPEC>
backend:
  replicas: <int>
  web_concurrency: <int>             # gunicorn/PM2 processes per pod
  worker_class: sync                 # sync | gthread | gevent (optional; default sync)
  worker_threads: <int>              # required when worker_class=gthread
  preload: true                      # optional; default true
  max_requests: <int>                 # optional; gunicorn worker recycle
  max_requests_jitter: <int>           # optional
  backlog: <int>                      # optional; listen backlog
  env:                               # optional allow-listed knobs only
    PG_POOL_MAX: "<int>"              # or DB_POOL_SIZE for SQLAlchemy apps
    # DB_POOL_OVERFLOW: "<int>"
    # GUNICORN_TIMEOUT: "<int>"
    # GUNICORN_KEEPALIVE: "<int>"
  resources:
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
  placement:
    workers: [<worker-name>, ...]   # optional; omit to allow all workers
    spread_replicas: true            # optional; default true
pooler:                              # optional; omit or enabled: false to connect apps directly to Postgres
  enabled: true
  mode: transaction                  # transaction | session
  replicas: <int>                    # optional; default 1
  max_client_conn: <int>
  default_pool_size: <int>
  min_pool_size: <int>               # optional
  reserve_pool_size: <int>           # optional
  service_name: pgbouncer            # optional; default pgbouncer
  resources:                         # optional
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
read_pooler:                         # optional; requires database.replicas > 1
  enabled: true
  mode: transaction
  replicas: <int>
  max_client_conn: <int>
  default_pool_size: <int>
  min_pool_size: <int>               # optional
  reserve_pool_size: <int>           # optional
  service_name: pgbouncer-read       # must differ from write pooler
  resources:                         # optional
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
cache:                               # optional application Redis
  enabled: true
  replicas: <int>                    # optional; default 1
  maxmemory: <quantity>              # e.g. 256Mi
  maxmemory_policy: allkeys-lru
  resources:                         # optional
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
database:
  enabled: true
  replicas: <int>                    # 1 = standalone; N>1 = 1 primary + (N-1) read replicas
  max_connections: <int>             # Postgres primary limit; pooler.default_pool_size must fit
  tuning:                            # optional; omit sub-keys to keep Postgres defaults
    shared_buffers: <quantity>       # e.g. 256Mi, 1Gi
    effective_cache_size: <quantity>
    work_mem: <quantity>             # e.g. 4Mi, 16Mi
    maintenance_work_mem: <quantity>
    max_parallel_workers_per_gather: <int>
    max_parallel_workers: <int>
    max_worker_processes: <int>
    random_page_cost: <float>        # e.g. 1.1 for SSD
    effective_io_concurrency: <int>  # e.g. 200 for SSD
    max_wal_size: <quantity>         # e.g. 1Gi
    checkpoint_timeout: <int>        # seconds
    wal_buffers: <quantity>
    jit_enabled: false
    statement_timeout_ms: <int>      # e.g. 30000
  resources:                         # default for all DB pods if primary/replica omitted
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
  primary:                           # optional; override primary pod only
    resources:
      cpu_request: <quantity>
      cpu_limit: <quantity>
      memory_request: <quantity>
      memory_limit: <quantity>
  replica:                           # optional; override read-replica pods only
    resources:
      cpu_request: <quantity>
      cpu_limit: <quantity>
      memory_request: <quantity>
      memory_limit: <quantity>
  cache:                             # optional database-adjacent Redis (DB_REDIS_URL)
    enabled: true
    use_shared: true                 # share root cache Redis; or false for dedicated redis-db
    maxmemory: <quantity>            # when use_shared: false
    maxmemory_policy: allkeys-lru
    resources:                         # when use_shared: false
      cpu_request: <quantity>
      cpu_limit: <quantity>
      memory_request: <quantity>
      memory_limit: <quantity>
  placement:
    worker: <worker-name>            # optional; exact pin (preferred for isolation)
    # workers: [<name>, ...]         # optional alternative; allow-list (pick one node)
</SPEC>
"""


def parse_spec_fragment(response: str) -> dict[str, Any]:
    match = _SPEC_BLOCK_RE.search(response)
    text = match.group(1).strip() if match else ""
    if not text:
        fences = _YAML_FENCE_RE.findall(response)
        if fences:
            text = fences[-1].strip()
    if not text:
        raise ValueError("Model response did not contain <SPEC> YAML or a ```yaml``` block")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Parsed spec fragment is not a YAML mapping")
    return data


def _parse_backend_placement(backend_raw: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    placement_raw = backend_raw.get("placement") or {}
    workers: list[str] = []
    spread = True
    if isinstance(placement_raw, dict):
        raw_workers = placement_raw.get("workers") or placement_raw.get("worker_nodes") or []
        if isinstance(raw_workers, (list, tuple)):
            workers = [str(w).strip() for w in raw_workers if str(w).strip()]
        spread = bool(placement_raw.get("spread_replicas", True))
    return tuple(workers), spread


def merge_fragment_into_spec(
    fragment: dict[str, Any],
    *,
    iteration_id: str,
    app_port: int,
    needs_db: bool,
    labels: dict[str, str],
) -> K8sWorkloadSpec:
    iid = normalize_iteration_id(iteration_id)
    backend_raw = fragment.get("backend") or {}
    if not isinstance(backend_raw, dict):
        raise ValueError("spec fragment must include backend mapping")

    db_raw = fragment.get("database") or {}
    if not isinstance(db_raw, dict):
        db_raw = {}

    placement_workers, spread_replicas = _parse_backend_placement(backend_raw)
    backend = BackendSpec.from_mapping(
        {
            **backend_raw,
            "image": backend_raw.get("image") or _IMAGE_PLACEHOLDER,
            "port": backend_raw.get("port") or app_port,
            "placement": {
                **(
                    backend_raw.get("placement")
                    if isinstance(backend_raw.get("placement"), dict)
                    else {}
                ),
                "workers": list(placement_workers),
                "spread_replicas": spread_replicas,
            },
        }
    )
    pooler_raw = fragment.get("pooler")
    pooler = PoolerSpec.from_mapping(
        pooler_raw if isinstance(pooler_raw, dict) else None
    )
    read_pooler_raw = fragment.get("read_pooler")
    read_pooler = (
        PoolerSpec.from_mapping(
            read_pooler_raw, default_service_name=DEFAULT_READ_POOLER_SERVICE
        )
        if isinstance(read_pooler_raw, dict)
        else PoolerSpec(enabled=False, service_name=DEFAULT_READ_POOLER_SERVICE)
    )
    cache_raw = fragment.get("cache")
    cache = (
        CacheSpec.from_mapping(cache_raw)
        if isinstance(cache_raw, dict)
        else CacheSpec()
    )
    database = DatabaseSpec.from_mapping(
        {
            "enabled": needs_db if needs_db else bool(db_raw.get("enabled", True)),
            **db_raw,
        }
        if needs_db
        else {"enabled": False}
    )
    return K8sWorkloadSpec(
        iteration_id=iid,
        namespace=default_k8s_namespace(iid),
        backend=backend,
        database=database,
        pooler=pooler,
        read_pooler=read_pooler,
        cache=cache,
        labels=dict(labels),
    )


_SPEC_ATTEMPT_META_FILENAME = "attempt.json"


def _write_spec_attempt_meta(
    attempt_dir: pathlib.Path,
    *,
    attempt_index: int,
    global_attempt_index: int,
    status: str,
    error: str | None,
    validation_feedback: str | None,
    note: str | None = None,
) -> None:
    """Persist ``attempts/<NNN>/attempt.json`` (one per LLM call)."""
    payload: dict[str, Any] = {
        "attempt_index": global_attempt_index,
        "validation_round": attempt_index,
        "status": status,
        "error": error,
        "validation_feedback": validation_feedback,
    }
    if note:
        payload["note"] = note
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / _SPEC_ATTEMPT_META_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _record_probe_failure_on_last_attempt(
    iteration_path: pathlib.Path,
    *,
    probe_reason: str,
    probe_feedback: str,
) -> None:
    """
    After a deploy probe fails, mark the **most recent** spec attempt as
    ``deploy_probe_failed`` so the on-disk record reflects the real outcome
    (the LLM produced a valid-looking spec, but the cluster wouldn't accept it).
    """
    attempts_dir = iteration_spec_attempts_dir(iteration_path)
    if not attempts_dir.is_dir():
        return
    best_idx = next_attempt_index(attempts_dir) - 1
    if best_idx < 1:
        return
    attempt_dir = attempt_subdir(attempts_dir, best_idx)
    meta_path = attempt_dir / _SPEC_ATTEMPT_META_FILENAME
    if not meta_path.is_file():
        return
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    data["status"] = "deploy_probe_failed"
    data["probe_reason"] = probe_reason
    data["probe_feedback"] = probe_feedback
    meta_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def generate_k8s_workload_spec(
    *,
    env: Env,
    scenario: Scenario,
    model: str,
    provider: str | None,
    temperature: float,
    reasoning_effort: str,
    safety_prompt: str,
    capacity: ClusterCapacity,
    app_hints: str,
    iteration_id: str,
    logger: logging.Logger,
    vllm_port: int = 8000,
    prior_feedback: IterationFeedback | str | None = None,
    validation_feedback: str | None = None,
    max_validation_retries: int = 3,
    sample_dir: pathlib.Path | None = None,
    iteration_path: pathlib.Path | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
    enable_attempts: bool = False,
    session: "Prompter | None" = None,
) -> tuple[K8sWorkloadSpec, str, list[str]]:
    """Call the configured LLM and return (spec, raw_response, validation_warnings).

    When ``enable_attempts`` is ``True`` (baseline iteration only), each LLM
    call writes a self-contained record under ``03-spec/attempts/<NNN>/``
    (``prompt.log``, ``response.log``, ``attempt.json``) so failed validation
    rounds stay auditable instead of being overwritten by the winning attempt.
    The winning attempt is also mirrored at the top of ``03-spec/``
    (``prompt.log``, ``response.log``) as required by
    :func:`write_spec_generation_artifacts` and the experiment summary writers.

    For refinement iterations (``enable_attempts=False``, the default) only
    the top-level ``03-spec/{prompt.log,response.log}`` files are written —
    no per-attempt forensics directory is created.
    """
    last_raw = ""
    validation_hint = validation_feedback
    for attempt in range(1, max_validation_retries + 1):
        prompt = build_k8s_spec_prompt(
            env=env,
            scenario=scenario,
            safety_prompt=safety_prompt,
            capacity=capacity,
            app_hints=app_hints,
            iteration_id=iteration_id,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            prior_feedback=prior_feedback,
            validation_feedback=validation_hint,
        )

        # Per-attempt directory: each LLM call lands its own
        # ``attempts/<NNN>/{prompt.log,response.log,attempt.json}``. Only
        # created for the baseline iteration (``enable_attempts=True``) —
        # refinement iterations write only the top-level prompt/response
        # logs and rely on git/snapshot diffs across iterations for
        # forensics. The global index (across both validation retries
        # inside a single call and across baseline deploy-probe rounds in
        # the outer loop) is read from disk, so a fresh
        # ``generate_baseline_spec_until_deployable`` iteration picks up
        # the next free slot automatically.
        per_attempt_dir: pathlib.Path | None = None
        global_attempt_idx = 0
        if iteration_path is not None:
            spec_dir = iteration_spec_dir(iteration_path)
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / PROMPT_LOG_FILENAME).write_text(
                prompt + "\n", encoding="utf-8"
            )
            if enable_attempts:
                attempts_dir = iteration_spec_attempts_dir(iteration_path)
                attempts_dir.mkdir(parents=True, exist_ok=True)
                global_attempt_idx = next_attempt_index(attempts_dir)
                per_attempt_dir = attempt_subdir(attempts_dir, global_attempt_idx)
                per_attempt_dir.mkdir(parents=True, exist_ok=True)
                (per_attempt_dir / PROMPT_LOG_FILENAME).write_text(
                    prompt + "\n", encoding="utf-8"
                )
        if session is not None:
            # Conversation mode: reuse the shared per-experiment Prompter so the
            # spec turn is appended to the running thread. Mirror the rollback
            # semantics of ``Prompter.send`` but keep the user turn long enough
            # to record an ``empty_response`` attempt below if needed.
            prompter = session
            prompter.append_user(prompt)
            if sample_dir is not None:
                from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

                check_k8s_llm_budget(sample_dir)
            try:
                responses = prompter.prompt_model(logger)
            except Exception:
                prompter.history.pop()
                raise
        else:
            prompter = Prompter(
                env=env,
                scenario=scenario,
                model=model,
                spec_type="openapi",
                safety_prompt=safety_prompt,
                batch_size=1,
                offset=0,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                vllm_port=vllm_port,
                provider=provider,
                use_stubs=False,
            )
            prompter.prompt = prompt
            if sample_dir is not None:
                from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

                check_k8s_llm_budget(sample_dir)
            responses = prompter.prompt_model(logger)
        if sample_dir is not None:
            record_k8s_llm_call(
                prompter=prompter,
                call_type="k8s_spec_generation",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=per_attempt_dir
                or (iteration_spec_dir(iteration_path) if iteration_path else None),
                iteration_id=iteration_id,
                note=f"validation_attempt={attempt} global_attempt={global_attempt_idx}",
            )
        if not responses:
            if session is not None and session.history:
                # Drop the dangling user turn so a retry re-appends cleanly.
                session.history.pop()
            if per_attempt_dir is not None:
                _write_spec_attempt_meta(
                    per_attempt_dir,
                    attempt_index=attempt,
                    global_attempt_index=global_attempt_idx,
                    status="empty_response",
                    error="LLM returned no completion for k8s spec generation",
                    validation_feedback=validation_hint,
                )
            raise RuntimeError("LLM returned no completion for k8s spec generation")
        last_raw = responses[0]
        if session is not None:
            session.append_assistant(last_raw)
        if per_attempt_dir is not None:
            (per_attempt_dir / RESPONSE_LOG_FILENAME).write_text(
                last_raw + "\n", encoding="utf-8"
            )
        # Persist the raw reply next to the prompt regardless of validation
        # outcome. ``write_spec_generation_artifacts`` only runs on success, so
        # without this a failed refinement spec leaves a ``prompt.log`` with no
        # ``response.log`` to inspect. On a passing attempt this is overwritten
        # with the identical accepted reply.
        if iteration_path is not None:
            (iteration_spec_dir(iteration_path) / RESPONSE_LOG_FILENAME).write_text(
                last_raw + "\n", encoding="utf-8"
            )
        try:
            fragment = parse_spec_fragment(last_raw)
            spec = merge_fragment_into_spec(
                fragment,
                iteration_id=iteration_id,
                app_port=env.port,
                needs_db=scenario.needs_db,
                labels={},
            )
        except ValueError as parse_exc:
            if per_attempt_dir is not None:
                _write_spec_attempt_meta(
                    per_attempt_dir,
                    attempt_index=attempt,
                    global_attempt_index=global_attempt_idx,
                    status="parse_failed",
                    error=str(parse_exc),
                    validation_feedback=validation_hint,
                )
            validation_hint = (
                f"Your previous response could not be parsed as a <SPEC> "
                f"YAML fragment: {parse_exc}. Re-emit the spec inside a "
                "single <SPEC>...</SPEC> block."
            )
            logger.warning(
                "spec validation attempt %d/%d failed (parse): %s",
                attempt,
                max_validation_retries,
                parse_exc,
            )
            continue
        spec, placement_errors = normalize_spec_placement(spec, capacity)
        if placement_errors:
            validation_hint = SpecValidationError(placement_errors).to_prompt_text()
            logger.warning(
                "spec validation attempt %d/%d failed (placement): %s",
                attempt,
                max_validation_retries,
                placement_errors,
            )
            if per_attempt_dir is not None:
                _write_spec_attempt_meta(
                    per_attempt_dir,
                    attempt_index=attempt,
                    global_attempt_index=global_attempt_idx,
                    status="placement_invalid",
                    error="; ".join(placement_errors),
                    validation_feedback=validation_hint,
                )
            continue

        result = validate_spec_against_cluster(
            spec, capacity, app_hints=app_hints
        )
        if result.errors:
            validation_hint = SpecValidationError(result.errors).to_prompt_text()
            logger.warning(
                "spec validation attempt %d/%d failed: %s",
                attempt,
                max_validation_retries,
                result.errors,
            )
            if per_attempt_dir is not None:
                _write_spec_attempt_meta(
                    per_attempt_dir,
                    attempt_index=attempt,
                    global_attempt_index=global_attempt_idx,
                    status="validation_failed",
                    error="; ".join(result.errors),
                    validation_feedback=validation_hint,
                )
            continue

        if attempt > 1:
            logger.info("spec validation passed on attempt %d", attempt)
        if per_attempt_dir is not None:
            _write_spec_attempt_meta(
                per_attempt_dir,
                attempt_index=attempt,
                global_attempt_index=global_attempt_idx,
                status="validation_passed",
                error=None,
                validation_feedback=validation_hint,
            )
        return spec, last_raw, result.warnings

    raise SpecValidationError(
        [f"Spec still invalid after {max_validation_retries} generation attempt(s)."]
        + [ln for ln in (validation_hint or "").splitlines() if ln.strip()][-8:]
    )


def write_spec_generation_artifacts(
    iteration_path: pathlib.Path,
    *,
    spec: K8sWorkloadSpec,
    raw_response: str,
    capacity: ClusterCapacity,
    warnings: list[str],
    logger: logging.Logger,
) -> pathlib.Path:
    ensure_iteration_core_layout(iteration_path)
    spec_path = iteration_spec_path(iteration_path)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec.write_yaml(spec_path)
    render_iteration(iteration_path)

    meta = {
        "spec_path": str(spec_path),
        "warnings": warnings,
        "cluster_capacity": capacity.to_prompt_dict(),
        "workload_spec": spec.to_yaml_dict(),
    }
    spec_dir = iteration_spec_dir(iteration_path)
    (spec_dir / "spec_gen.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (spec_dir / RESPONSE_LOG_FILENAME).write_text(
        raw_response + "\n",
        encoding="utf-8",
    )
    if warnings:
        for w in warnings:
            logger.warning("spec validation: %s", w)
    logger.info("Wrote %s and rendered manifests", spec_path)
    return spec_path


def reuse_deployment_spec_for_iteration(
    *,
    iteration_path: Path,
    sample_dir: Path,
    source_iteration_id: str,
    target_iteration_id: str,
    extra_labels: dict[str, str] | None = None,
    logger: logging.Logger,
) -> Path:
    """
    Copy deployment parameters from a prior iteration (no spec LLM).

    Used after successful **code** refinement: bench the new image under the
    same replicas/resources/DB settings as the iteration we learned from.
    """
    source_path = resolve_iteration_dir(sample_dir, source_iteration_id)
    src_spec_path = find_iteration_spec_path(source_path)
    if src_spec_path is None:
        raise FileNotFoundError(
            f"No spec to reuse under {source_path} (from {source_iteration_id!r})"
        )

    spec = K8sWorkloadSpec.from_yaml_file(src_spec_path)
    iid = normalize_iteration_id(target_iteration_id)
    labels = dict(spec.labels)
    if extra_labels:
        labels.update(extra_labels)

    reused = K8sWorkloadSpec(
        iteration_id=iid,
        namespace=default_k8s_namespace(iid),
        backend=spec.backend,
        database=spec.database,
        pooler=spec.pooler,
        read_pooler=spec.read_pooler,
        cache=spec.cache,
        labels=labels,
    )

    ensure_iteration_core_layout(iteration_path)
    dest = iteration_spec_path(iteration_path)
    reused.write_yaml(dest)
    render_iteration(iteration_path)

    note = (
        f"Reused deployment spec from {source_path.name} ({source_iteration_id})\n"
        f"Target iteration: {iid}\n"
        "No LLM spec generation (code-only refinement phase).\n"
    )
    (iteration_spec_dir(iteration_path) / "reused_from.txt").write_text(
        note, encoding="utf-8"
    )
    logger.info(
        "Reused deployment spec from %s → %s (no LLM spec generation)",
        source_path,
        dest,
    )
    return dest


def generate_k8s_specs_for_task(
    task: Any,
    results_dir: Path,
    samples: list[int],
    force: bool,
    *,
    k8s_iteration: str | None = None,
    iteration_path: Path | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
    prior_feedback: Any | None = None,
    iteration_index: int = 1,
) -> list[Path]:
    """LLM spec per sample. Callers must run ``functional_tests_gate`` before calling."""
    from tasks import esc

    written: list[Path] = []
    capacity = collect_cluster_capacity()

    for sample in samples:
        sample_dir = task.get_sample_dir(results_dir, sample)
        iid = normalize_iteration_id(
            k8s_iteration
            or os.environ.get("BAXBENCH_K8S_ITERATION")
            or new_iteration_id(sample_dir)
        )
        if iteration_path is None:
            from k8s_bench.workspace import resolve_iteration_dir

            iteration_path = resolve_iteration_dir(sample_dir, iid)
            if not iteration_path.is_dir():
                iteration_path = task.get_k8s_iteration_dir(results_dir, sample, iid)
        ensure_iteration_core_layout(iteration_path)
        spec_path = iteration_spec_path(iteration_path)
        regen = force or iteration_index > 0
        if find_iteration_spec_path(iteration_path) is not None and not regen:
            existing = find_iteration_spec_path(iteration_path)
            assert existing is not None
            logging.getLogger(task.id).info(
                "sample%d: spec exists at %s (use --force to regenerate)",
                sample,
                existing,
            )
            written.append(existing)
            continue

        log_file = iteration_spec_dir(iteration_path) / "phase.log"
        with task.create_logger(log_file) as logger:
            code_dir = latest_code_dir(
                task.get_sample_dir(results_dir, sample),
                fallback=task.get_code_dir(results_dir, sample),
            )
            app_hints = _read_app_hints(code_dir)
            labels = {
                "baxbench.dev/model": esc(task.model),
                "baxbench.dev/scenario": esc(task.scenario.id),
                "baxbench.dev/env": esc(task.env.id),
                "baxbench.dev/spec-gen": "true",
            }

            from ..session import get_experiment_session, persist_session

            spec_session = get_experiment_session(
                task, sample_dir, sample, vllm_port=vllm_port, logger=logger
            )
            retries = 0
            while True:
                try:
                    spec, raw, warnings = generate_k8s_workload_spec(
                        env=task.env,
                        scenario=task.scenario,
                        model=task.model,
                        provider=task.provider,
                        temperature=task.temperature,
                        reasoning_effort=task.reasoning_effort,
                        safety_prompt=task.safety_prompt,
                        capacity=capacity,
                        app_hints=app_hints,
                        iteration_id=iid,
                        logger=logger,
                        vllm_port=vllm_port,
                        prior_feedback=prior_feedback,
                        sample_dir=sample_dir,
                        iteration_path=iteration_path,
                        session=spec_session,
                    )
                    persist_session(spec_session, sample_dir, logger=logger)
                    spec = K8sWorkloadSpec(
                        iteration_id=spec.iteration_id,
                        namespace=spec.namespace,
                        backend=BackendSpec.from_mapping(
                            {
                                "image": spec.backend.image,
                                "replicas": spec.backend.replicas,
                                "port": task.env.port,
                                "web_concurrency": spec.backend.web_concurrency,
                                "worker_class": spec.backend.worker_class,
                                "worker_threads": spec.backend.worker_threads,
                                "preload": spec.backend.preload,
                                "max_requests": spec.backend.max_requests,
                                "max_requests_jitter": spec.backend.max_requests_jitter,
                                "backlog": spec.backend.backlog,
                                "resources": {
                                    "cpu_request": spec.backend.resources.cpu_request,
                                    "cpu_limit": spec.backend.resources.cpu_limit,
                                    "memory_request": spec.backend.resources.memory_request,
                                    "memory_limit": spec.backend.resources.memory_limit,
                                },
                                "env": dict(spec.backend.env),
                                "placement": {
                                    "workers": list(spec.backend.placement_workers),
                                    "spread_replicas": spec.backend.spread_replicas,
                                },
                            }
                        ),
                        database=spec.database,
                        pooler=spec.pooler,
                        read_pooler=spec.read_pooler,
                        cache=spec.cache,
                        labels={**spec.labels, **labels},
                    )
                    out = write_spec_generation_artifacts(
                        iteration_path,
                        spec=spec,
                        raw_response=raw,
                        capacity=capacity,
                        warnings=warnings,
                        logger=logger,
                    )
                    try:
                        from ..experiment_summary import append_spec_generation_block

                        summary_path = append_spec_generation_block(
                            sample_dir=sample_dir,
                            iteration_id=iid,
                            iteration_path=iteration_path,
                            spec=spec,
                            raw_response=raw,
                            warnings=warnings,
                            had_prior_feedback=prior_feedback is not None,
                            iteration_index=iteration_index,
                        )
                        logger.info("Updated experiment summary: %s", summary_path)
                    except Exception as exc:
                        logger.warning("Could not update experiment summary: %s", exc)
                    written.append(out)
                    break
                except SpecValidationError as e:
                    logger.error(
                        "k8s spec validation failed for sample %d after LLM retries: %s",
                        sample,
                        e,
                    )
                    break
                except Exception as e:
                    retries += 1
                    if retries > max_retries:
                        logger.exception(
                            "k8s spec generation failed for sample %d: %s",
                            sample,
                            e,
                            exc_info=e,
                        )
                        break
                    delay = min(base_delay * 2**retries, max_delay)
                    logger.warning(
                        "k8s spec gen retry %d/%d after %s (sleep %.1fs)",
                        retries,
                        max_retries,
                        e,
                        delay,
                    )
                    time.sleep(delay)

    return written


def _apply_task_labels_to_spec(
    spec: K8sWorkloadSpec,
    *,
    task: Any,
    results_dir: Path,
    sample: int,
) -> K8sWorkloadSpec:
    from tasks import esc

    labels = {
        "baxbench.dev/model": esc(task.model),
        "baxbench.dev/scenario": esc(task.scenario.id),
        "baxbench.dev/env": esc(task.env.id),
        "baxbench.dev/spec-gen": "true",
    }
    return K8sWorkloadSpec(
        iteration_id=spec.iteration_id,
        namespace=spec.namespace,
        backend=BackendSpec.from_mapping(
            {
                "image": spec.backend.image,
                "replicas": spec.backend.replicas,
                "port": task.env.port,
                "web_concurrency": spec.backend.web_concurrency,
                "worker_class": spec.backend.worker_class,
                "worker_threads": spec.backend.worker_threads,
                "preload": spec.backend.preload,
                "max_requests": spec.backend.max_requests,
                "max_requests_jitter": spec.backend.max_requests_jitter,
                "backlog": spec.backend.backlog,
                "resources": {
                    "cpu_request": spec.backend.resources.cpu_request,
                    "cpu_limit": spec.backend.resources.cpu_limit,
                    "memory_request": spec.backend.resources.memory_request,
                    "memory_limit": spec.backend.resources.memory_limit,
                },
                "env": dict(spec.backend.env),
                "placement": {
                    "workers": list(spec.backend.placement_workers),
                    "spread_replicas": spec.backend.spread_replicas,
                },
            }
        ),
        database=spec.database,
        pooler=spec.pooler,
        read_pooler=spec.read_pooler,
        cache=spec.cache,
        labels={**spec.labels, **labels},
    )


def generate_and_write_spec(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    iteration_id: str,
    logger: logging.Logger,
    capacity: ClusterCapacity,
    prior_feedback: IterationFeedback | None = None,
    validation_feedback: str | None = None,
    max_validation_retries: int = 1,
    iteration_index: int = 0,
    total_iterations: int = 0,
    vllm_port: int = 8000,
    enable_attempts: bool = False,
) -> tuple[Path | None, str | None]:
    """
    Generate spec via LLM, static-validate, write artifacts.

    Returns ``(spec_path, error_message)``. ``error_message`` is set when static
    validation fails after ``max_validation_retries`` attempt(s).
    """
    sample_dir = task.get_sample_dir(results_dir, sample)
    code_dir = latest_code_dir(
        sample_dir, fallback=task.get_code_dir(results_dir, sample)
    )
    app_hints = _read_app_hints(code_dir)
    from ..session import get_experiment_session, persist_session

    spec_session = get_experiment_session(
        task, sample_dir, sample, vllm_port=vllm_port, logger=logger
    )
    try:
        spec, raw, warnings = generate_k8s_workload_spec(
            env=task.env,
            scenario=task.scenario,
            model=task.model,
            provider=task.provider,
            temperature=task.temperature,
            reasoning_effort=task.reasoning_effort,
            safety_prompt=task.safety_prompt,
            capacity=capacity,
            app_hints=app_hints,
            iteration_id=iteration_id,
            logger=logger,
            vllm_port=vllm_port,
            prior_feedback=prior_feedback,
            validation_feedback=validation_feedback,
            max_validation_retries=max_validation_retries,
            sample_dir=sample_dir,
            iteration_path=iteration_path,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            enable_attempts=enable_attempts,
            session=spec_session,
        )
        persist_session(spec_session, sample_dir, logger=logger)
        spec = _apply_task_labels_to_spec(
            spec, task=task, results_dir=results_dir, sample=sample
        )
        out = write_spec_generation_artifacts(
            iteration_path,
            spec=spec,
            raw_response=raw,
            capacity=capacity,
            warnings=warnings,
            logger=logger,
        )
        try:
            from ..experiment_summary import append_spec_generation_block

            append_spec_generation_block(
                sample_dir=sample_dir,
                iteration_id=iteration_id,
                iteration_path=iteration_path,
                spec=spec,
                raw_response=raw,
                warnings=warnings,
                had_prior_feedback=prior_feedback is not None,
                iteration_index=iteration_index,
            )
        except Exception as exc:
            logger.warning("Could not update experiment summary: %s", exc)
        return out, None
    except SpecValidationError as exc:
        return None, exc.to_prompt_text()
    except Exception as exc:
        logger.exception("spec generation failed: %s", exc, exc_info=exc)
        return None, str(exc)


def generate_baseline_spec_until_deployable(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    iteration_id: str,
    logger: logging.Logger,
    deploy_probe: Any,
    iteration_index: int = 0,
    total_iterations: int = 0,
    vllm_port: int = 8000,
    max_deploy_attempts: int | None = None,
) -> tuple[Path | None, str | None]:
    """
    Baseline (iteration-000): retry spec generation until deploy probe passes.

    ``deploy_probe`` is a zero-arg callable returning ``DeployProbeResult``.
    """
    if max_deploy_attempts is None:
        max_deploy_attempts = int(
            os.environ.get("BAXBENCH_K8S_BASELINE_SPEC_MAX_ATTEMPTS", "5")
        )
    capacity = collect_cluster_capacity()
    validation_feedback: str | None = None
    last_error = "baseline spec generation did not produce a deployable configuration"

    for attempt in range(1, max_deploy_attempts + 1):
        logger.info(
            "baseline spec attempt %d/%d for sample %d",
            attempt,
            max_deploy_attempts,
            sample,
        )
        spec_path, gen_error = generate_and_write_spec(
            task=task,
            results_dir=results_dir,
            sample=sample,
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            logger=logger,
            capacity=capacity,
            prior_feedback=None,
            validation_feedback=validation_feedback,
            enable_attempts=True,
            max_validation_retries=3,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            vllm_port=vllm_port,
        )
        if spec_path is None:
            last_error = gen_error or last_error
            validation_feedback = gen_error
            continue

        probe = deploy_probe()
        if probe.ok:
            logger.info(
                "baseline deploy probe passed on attempt %d for sample %d",
                attempt,
                sample,
            )
            return spec_path, None

        last_error = probe.reason
        validation_feedback = probe.to_prompt_feedback()
        # Reach back into the spec ``attempts/`` log and mark the validation
        # attempt that just produced this spec as ``deploy_probe_failed`` —
        # the LLM produced a structurally valid spec but the cluster couldn't
        # bring it up, and that distinction matters when debugging the chain.
        _record_probe_failure_on_last_attempt(
            iteration_path,
            probe_reason=probe.reason,
            probe_feedback=validation_feedback,
        )
        logger.warning(
            "baseline deploy probe failed attempt %d/%d: %s",
            attempt,
            max_deploy_attempts,
            probe.reason,
        )

    return None, last_error


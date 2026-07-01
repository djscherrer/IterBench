"""Prompt builders for k8s workload spec LLM generation."""

from __future__ import annotations

import pathlib
import re

from env.base import Env
from scenarios.base import Scenario

from ..cluster.capacity import ClusterCapacity, capacity_as_json
from ..prompt_helpers import ArtifactPointers, format_artifact_pointers_block

def _benchmark_load_hint(scenario: Scenario) -> str:
    """Scenario-specific load description (replaces stale generic endpoint examples)."""
    return (
        f"The deployment will be exercised by adaptive Locust load testing on scenario "
        f"**{scenario.id}** (sustained mixed HTTP traffic against the API). Size replicas "
        "and resources to maximize **goodput** (sustained rate of **successful** HTTP "
        "responses) under that pressure. Raw throughput that comes with elevated error "
        "rates is NOT a win — failed requests do not count."
    )


def format_iteration_progress(
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


def _scenario_performance_guidance(
    env: Env, scenario: Scenario, safety_prompt: str
) -> str:
    """One-line high-performance instruction from the scenario safety prompt."""
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


def build_k8s_spec_prompt(
    *,
    env: Env,
    scenario: Scenario,
    safety_prompt: str,
    capacity: ClusterCapacity,
    iteration_id: str,
    iteration_index: int = 0,
    total_iterations: int = 0,
    refinement: bool = False,
    validation_feedback: str | None = None,
    artifact_pointers: ArtifactPointers | None = None,
) -> str:
    performance_guidance = _scenario_performance_guidance(env, scenario, safety_prompt)
    progress = format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    if refinement:
        pointer_block = (
            format_artifact_pointers_block(
                artifact_pointers, include_bench_telemetry=True
            )
            if artifact_pointers is not None
            else "(artifact pointers unavailable)"
        )
        goal = f"""## Goal
You are refining deployment parameters for iteration `{iteration_id}` after a benchmark of the **previous** iteration.

**Progress**: {progress} Plan your remaining budget — bold experiments early, consolidate refinements toward the end.

**Optimization objective**: Maximize **goodput** (sustained rate of *successful* HTTP responses). Failed requests do not count. Raw throughput with high error rates is NOT a win.

Use the **Benchmark telemetry** pointer below (decision-phase turn in conversation history) when tuning levers — do not expect Locust or diagnostics to be repeated in this message.

{performance_guidance}

## Context
- Scenario: {scenario.id}
- Environment: {env.id} (listen port {env.port})
- Iteration: {iteration_id}

## Conversation history

{pointer_block}
"""
    else:
        goal = f"""## Goal
Propose deployment parameters for iteration `{iteration_id}` so the application can sustain **very high concurrent user load** in a Locust benchmark.

**Progress**: {progress}

**Optimization objective**: Maximize **goodput** (successful responses per second). Failed requests do not count toward your score.

{performance_guidance}
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
- Each backend pod runs a single app process; scale throughput with `backend.replicas` and per-pod `resources`. DB connection pool sizing is controlled by `backend.env.PG_POOL_MAX` / `backend.env.DB_POOL_SIZE` (if you set them).

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
2. **Cluster budget**: sum of all pod requests (backends + primary + read replicas + `pooler.replicas` + `read_pooler.replicas` (if enabled) + `cache.replicas` + dedicated `database.cache` Redis if `use_shared: false`) must fit cluster capacity after reserve.
3. Optional **placement**: restrict or pin which workers may run postgres/backends; `spread_replicas: true` spreads backend pods across nodes.
4. Use worker **`name` values** from the per-worker list (short names like `node3` are accepted).
5. **DB connection budget is enforced when explicit**: if you set `backend.env.PG_POOL_MAX` or `backend.env.DB_POOL_SIZE`, the framework estimates app-side DB client connections as `backend.replicas × pool_max`. If `pooler.enabled`, then `pooler.max_client_conn` (and `read_pooler.max_client_conn` if enabled) must be **≥** that estimate, or the spec will be rejected before deploy. Lower replicas, pool_max, or raise `max_client_conn`.

## Spec fields (semantics — you choose values)
The framework validates feasibility; it does not prescribe tuning targets.

**`backend`** (horizontally scalable — many stateless pods):
- `replicas`: pod count behind the Service (primary scaling lever — one app process per pod)
- `env`: optional **allow-listed** tuning knobs (see below). Framework sets `DB_*`, `PORT`, `REDIS_URL`, `DB_REDIS_URL`; do not duplicate those.
- `resources`: per-pod CPU/memory requests & limits (scheduling uses **requests**)
- `placement.workers`: optional node allow-list (omit = any worker)
- `placement.spread_replicas`: prefer spreading pods across nodes (default true)

**`pooler`** (PgBouncer — optional connection multiplexer in front of Postgres **primary**):
- Sits between app pods and the primary. Framework sets `DB_HOST`/`DB_PORT` to the pooler when enabled.
- `enabled`: `true` to deploy PgBouncer (recommended when scaling `replicas` risks exhausting `max_connections`).
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



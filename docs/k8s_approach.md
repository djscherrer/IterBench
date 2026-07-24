# Kubernetes-Aware Deployment Optimization for BaxBench

## Idea

Extend BaxBench so that the agent not only generates application code, but also decides how the backend is deployed in Kubernetes under fixed cluster constraints.

The focus is not arbitrary YAML generation, but adaptive deployment optimization:
- resource allocation,
- replica count,
- placement decisions,
- throughput vs latency tradeoffs.

---

# Phase 1: Deployment Configuration

Initially:
- database remains external,
- framework handles infrastructure/networking,
- agent only controls a constrained deployment search space.

Agent input:
- generated backend/scenario,
- available cluster nodes/resources,
- benchmark objective.

Agent output (high-level config, not raw YAML):

```yaml
backend:
  replicas: 6
  cpu_request: 500m
  memory_request: 512Mi
  cpu_limit: 1
  memory_limit: 1Gi
```

Framework then generates:
- Kubernetes Deployment,
- Service,
- namespace/configuration,
- deployment orchestration.

Goal:
- keep search space manageable,
- avoid invalid YAML,
- focus on deployment reasoning rather than syntax generation.

---

# Phase 2: Benchmarking + Feedback Loop

Workflow:

```text
agent proposes deployment
→ deploy to cluster
→ run Locust benchmark
→ collect telemetry
→ feed results back to agent
→ allow refinement iteration
```

Adaptive load profile:
- gradually increase load until SLA breaks,
- measure sustainable throughput.

Feedback signals:
- RPS,
- p95 latency,
- error rate,
- CPU/memory utilization,
- throttling,
- OOMs,
- pod failures,
- scheduling issues.

Agent gets limited refinement attempts (e.g. 2–3).

Goal:
- maximize throughput under fixed cluster resources and SLA constraints.

---

# Phase 3: Dynamic Environments

After static optimization works:

Possible extensions:
- random pod failures,
- bursty load spikes,
- heterogeneous nodes,
- more complex multi-service workloads.

This makes:
- placement,
- resilience,
- and adaptation strategies
more important.

---

# Important Design Choice

Framework controls:
- infrastructure,
- networking,
- deployment orchestration.

Agent controls:
- deployment strategy parameters only.

This keeps:
- experiments reproducible,
- search space constrained,
- and evaluation stable.

---

# Proposed Initial Steps

1. Lab Kubernetes setup (kubeadm cluster + kubeconfig)
2. Manual deployment experiments
3. Config → YAML generation layer
4. Automated deploy + Locust pipeline
5. Telemetry collection
6. Iterative optimization loop
7. Dynamic load / failure scenarios

---

# K8s iterative benchmarking (implemented)

**`--mode k8s-bench`** (default) runs the full loop per phase:

1. **Phase 000 (baseline)** — LLM codegen with FT retries, then spec / deploy / bench
2. **Phases 001–N (refinement)** — LLM chooses code vs deployment tuning; **single attempt** per refinement phase (fail-fast on spec/deploy/bench; code baseline still retries)
3. **Deploy probe** — static spec validation, then `kubectl apply` + Ready waits + Service Endpoints + NodePort resolution
4. **Feedback** written to `05-bench/iteration_feedback.json` for the next phase (successful bench only)

Orchestration lives in `src/k8s_bench/orchestration/execute.py`, which calls one stage module per numbered folder.

### Stage modules and entry points

| Folder | Module | Main callables |
|--------|--------|----------------|
| `01-decision/` | `stages/decision.py` | `run_decision_stage` — code vs spec refinement routing |
| `02-code/` | `stages/code.py` | `run_code_stage`, `run_reuse_code_stage` — LLM codegen + functional tests |
| `03-spec/` | `stages/spec.py` | `run_spec_stage`, `run_reuse_spec_stage` — produce / reuse `spec.yaml` |
| `04-deploy/` | `stages/deploy.py` | `run_deploy_stage`, `update_iteration_spec`, `check_service_endpoints_ready` |
| `05-bench/` | `stages/bench.py` | `run_bench_stage`, `run_distributed_locust`, `persist_successful_bench_feedback` — Locust + feedback/summary on success |

Top-level entry: `k8s_bench/loop.py` → `run_k8s_bench`. Deploy-only mode uses `resolve_iterations_to_run` in the same file.

Cluster mechanics: `k8s_bench/cluster/deploy.py` (`deploy_iteration`, `DeployResult`, `write_deploy_record`).

Paths / workspace: `workspace/paths.py` (`make_k8s_perf_run_dir`, `deploy_probe_record_path`, …).

**Removed:** `k8s_bench/iteration.py` (former grab-bag). Bench engine, deploy overlay, and iteration resolution now live in the stage modules above.

---

### Spec vs deploy vs bench — artifact ownership

Three layers; do not conflate them.

| Artifact | Written by | Contents | Mutated after spec stage? |
|----------|------------|----------|---------------------------|
| **`03-spec/spec.yaml`** | Spec stage (LLM) | Workload **plan**: replicas, resources, pooler, DB topology, cache. Image may be placeholder (`baxbench/pending-at-bench:latest`). | **No** — stays the LLM record |
| **`04-deploy/manifests/all.yaml`** | Deploy (`write_manifest_files`) | Rendered K8s YAML from **in-memory overlay** (LLM spec + runtime fields) | N/A (regenerated each deploy) |
| **`04-deploy/probe.json`** | Deploy (on success) | **Runtime snapshot** + apply result | Written once per successful deploy |

**Deploy overlay (in memory only)** — `update_iteration_spec()` in `stages/deploy.py`:

- Reads `03-spec/spec.yaml` (raises if missing — no silent default spec).
- Merges deploy-time fields: registry `image`, framework `port`, `needs_db` guard, deploy `labels`.
- Does **not** rewrite `spec.yaml` on disk.
- Passes merged `K8sWorkloadSpec` to `deploy_iteration(spec=…)`.

**`probe.json` runtime fields** (in addition to `success`, `namespace`, `wait_details`, …):

| Field | Meaning |
|-------|---------|
| `image_reference` | Pushed registry tag for the FT-built image |
| `backend_port` | App port used in manifests / Locust |
| `nodeport_target` | External URL for load generators (`http://<node>:<nodePort>`) |
| `deploy_labels` | BaxBench metadata labels applied at deploy |

Bench **must not** re-derive these; it reads them from `probe.json` via `load_probe_deploy_result()`. Missing runtime fields → error (“re-run deploy”).

Bench still reads **`spec.yaml`** for one purpose only: **diagnostics topology** (which DB/pooler/cache services exist) for `diagnostics_session_for_k8s`. That information is not stored in the probe.

---

### Deploy stage flow (`04-deploy`)

```text
prepare_image_for_k8s
  → update_iteration_spec (in-memory overlay)
  → deploy_iteration(spec=overlay, write_record=False)
       → write_manifest_files → kubectl apply → wait Ready
  → check_service_endpoints_ready (backend)
  → resolve_nodeport_target
  → optional DB service endpoint checks
  → DeployResult.with_runtime(...) → write_deploy_record (probe.json)
```

---

### Bench stage flow (`05-bench`)

```text
run_bench_stage
  → run_bench_attempt
       → for each performance_tests name (CSV prefix only):
            run_distributed_locust
              → load_probe_deploy_result (runtime from probe.json)
              → load spec.yaml (diagnostics topology only)
              → check_service_endpoints_ready (sanity: pods still up)
              → DistributedLocustSession + diagnostics_session_for_k8s
       → refresh_plots_after_bench
```

`run_distributed_locust` (formerly `run_k8s_bench_iteration`) is the Locust + diagnostics engine. It does not apply manifests or push images.

`05-bench/config.json` embeds the LLM `spec.yaml` dict plus full `deploy_result` (including runtime fields) for post-run inspection.

---

### Experiment slug and layout

Set `K8S_EXPERIMENT` / `--k8s-experiment` /
`BAXBENCH_K8S_EXPERIMENT` to group iterative state under
`sampleN/k8s-experiments/<slug>/iterations/…`. When unset, the slug
`default` is used. Initial generated code and functional tests stay at
`sampleN/code/` and `sampleN/functional_tests/` (immutable after initial codegen).
Refined code and iteration-scoped FT artifacts live under
``iterations/iteration-NNN/02-code/`` only.

Each iteration directory layout:

```
iterations/iteration-000-baseline/
  meta.json
  iteration.log
  01-decision/
  02-code/                    # codegen + functional_tests (+ attempts/ on retry)
  03-spec/spec.yaml           # LLM workload plan (not patched by deploy)
  04-deploy/
    probe.json                # deploy runtime snapshot (sole deploy record)
    manifests/all.yaml
    phase.log
  05-bench/
    bench.log
    config.json
    iteration_feedback.json
    locust/…
    diagnostics/…

iterations/iteration-001-spec/      # deployment/spec refinement
iterations/iteration-002-code/      # code refinement
iterations/iteration-003-spec-failed/  # failed phase (excluded from feedback)
```

**Namespace cleanup** (always on): before each deploy and after each bench run,
all `baxbench-*` Kubernetes namespaces are deleted automatically (frees cluster
CPU; results on disk are kept). Set `BAXBENCH_K8S_CLEANUP=false` only to disable.

**Spec validation**: LLM specs are checked against **per-worker** capacity (each
pod must fit on one node using **requests**), connection pool budget
(`replicas × pool_max ≤ database.max_connections`), and cluster request totals.
**Baseline (000)** re-prompts the LLM (static validation + deploy probe) until
deployable or `BAXBENCH_K8S_BASELINE_SPEC_MAX_ATTEMPTS` (default 5). **Refinement
phases** use a single spec attempt; static validation or deploy probe failure marks
the phase as ``iteration-NNN-spec-failed`` (excluded from the feedback chain).

Optional spec fields: `database.replicas` (1 = standalone; N>1 = primary + read
replicas), `database.max_connections`, `database.placement.worker` (exact pin) or
`database.placement.workers` (allow-list), `backend.placement.workers`,
`backend.placement.spread_replicas` (rendered as node affinity / pod anti-affinity).

The LLM prompt describes **field semantics and hard constraints** only — no
recommended numeric ranges — so the agent must learn tuning from benchmark feedback.

### Spec reference (agent-controlled)

**Backend** — horizontally scalable; many pods behind one Service.

| Field | Purpose |
|-------|---------|
| `replicas` | Pod count; primary throughput knob for stateless apps |
| `resources.*` | Per-pod CPU/memory requests & limits |
| `placement.workers` | Optional node allow-list (omit = any worker) |
| `placement.spread_replicas` | Prefer spreading pods across nodes (default true) |

**Database** — Postgres primary/replica topology.

| Field | Purpose |
|-------|---------|
| `replicas` | `1` = single pod; `N>1` = 1 primary + (N−1) read replicas |
| `max_connections` | Primary connection limit; must cover `backend.replicas × pool_size` |
| `resources.*` | Per DB pod (primary + each replica); requests must fit one worker each |
| `placement.worker` | Pin all DB pods to one node (if combined requests fit) |
| `placement.workers` | Allow-list of nodes for DB pods |

### Postgres on Kubernetes (how replication works here)

Production Postgres on K8s is almost always **primary + read replicas**, not
multi-master active-active:

- **Primary** accepts reads and writes; the generated BaxBench app connects here
  via the `postgres` Service.
- **Read replicas** stream WAL from the primary (async replication by default).
  Exposed via `postgres-read` Service; unused unless the app is taught read/write
  split.
- **Synchronization**: replicas catch up by replaying the primary's WAL log.
  Lag is normal under load; synchronous replication is rare outside strict HA setups.
- **Operators** (CloudNativePG, Zalando, Crunchy) manage this in production; BaxBench
  uses Bitnami Postgres with master/slave env vars when `database.replicas > 1`.
- **`replicas: 1`** still uses the simple official Postgres image (Deployment).

Future extensions: persistent volumes (StatefulSet primary), PgBouncer pooler,
synchronous replication for HA failover.

**Deployment vs code refinement** (phase 001+): before each refinement phase, an LLM may
choose to refine the **deployment spec** (default path) or **application code**
(benchmark feedback appended to the normal codegen prompt, then functional tests
via `Task.test_code`). **Single attempt** per refinement phase — functional test or
deploy probe failure renames the folder to `NNN-code-failed` / `NNN-spec-failed`,
reverts live code to the last passing snapshot, and leaves `prior_feedback` unchanged.
Control with `--k8s-refinement auto|deployment|code` (default `auto`).

After **generate** + **test**:

```bash
./scripts/bench_k8s.sh
# or: --k8s-iterations 10  for iteration-000 baseline + iteration-001 .. iteration-010
```

### CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--k8s-iterations N` | 1 | Baseline `iteration-000` plus N refinement phases (`001`…`NNN`) |
| `--deploy-only` | off | Deploy+bench existing iterations only (no LLM refinement) |
| `--k8s-iteration iteration-000` | — | Pin a single phase (ignores N) |
| `--k8s-experiment adaptive-may20` | — | Workspace under `k8s-experiments/<slug>/` |
| `--force` | — | Regenerate specs and re-bench |

Phase 1 prompt: scenario, framework, `high_performance`, app code excerpt, cluster capacity.

Phase 2+ prompt adds **feedback**: Locust per-endpoint stats as a markdown table
(from ``locust/results/<test>_stats.csv``), Locust top errors, Kubernetes pod/node
utilization aggregated over the run (min/avg/max from
``diagnostics/kubernetes/cluster/kubectl_top_*.csv``), previous ``03-spec/spec.yaml``. The full LLM prompt is logged in
``iterations/iteration-NNN/03-spec/prompt.log``; feedback-only text is in
``iterations/iteration-NNN/05-bench/iteration_feedback.json`` (and ``.txt`` when generated).

**Experiment trajectory file**: each workspace maintains ``experiment_summary.md``
(append-only). After every spec generation it records deployment, diff vs the
previous iteration, and LLM rationale (from ``03-spec/response.log``).
After every Locust run it records time range, an adaptive ramp table (from
``bench.log``), aggregate req/fail stats, and top errors.

Standalone spec-only (no deploy): use ``--mode k8s-bench`` with ``--k8s-iteration``
to pin a single phase, or edit ``03-spec/spec.yaml`` by hand and re-bench with
``--deploy-only``.

### Code-stage infrastructure vs application failures

Functional-test harness classifies some log lines as **infrastructure** (port bind failures, harness could not start container, etc.). **Application crashes** (e.g. gunicorn `ModuleNotFoundError` in container logs) that prevent the HTTP server from starting are **code failures**, not infra — so baseline codegen can retry with FT feedback. See `k8s_bench/failure/infra.py` and `stages/code.py` fail-fast rules.
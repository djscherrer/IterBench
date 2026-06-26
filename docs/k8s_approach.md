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

1. **Phase 000 (baseline)** — LLM spec with deploy-probe retries until the cluster accepts the layout, then Locust benchmark
2. **Phases 001–N (refinement)** — LLM chooses code vs deployment tuning; **single attempt** per phase (fail-fast)
3. **Deploy probe** (spec paths) — static validation, then `kubectl` Ready checks + Service Endpoints (no HTTP traffic)
4. **Feedback** written to `iterations/NNN/bench/iteration_feedback.json` for the next phase

**Experiment slug**: set `K8S_EXPERIMENT` / `--k8s-experiment` /
`BAXBENCH_K8S_EXPERIMENT` to group iterative state under
`sampleN/k8s-experiments/<slug>/iterations/iteration-000/…`. When unset, the slug
`default` is used. Initial generated code and functional tests stay at
`sampleN/code/` and `sampleN/functional_tests/` (immutable after initial codegen).
Refined code and iteration-scoped FT artifacts live under
``iterations/iteration-NNN/code/`` only.

Each iteration directory layout:

```
iterations/iteration-000/           # created at phase start
iterations/iteration-000-baseline/  # after baseline kind is known
  meta.json
  spec/
  manifests/
  deploy/probe.json
  deploy/bench.json
  bench/

iterations/iteration-001-spec/      # deployment/spec refinement
iterations/iteration-002-code/      # code refinement
iterations/iteration-003-spec-failed/  # failed phase (excluded from feedback)
  decision/
  code/
  functional_tests/
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
``diagnostics/kubernetes/cluster/kubectl_top_*.csv``), previous ``spec.yaml``. The full LLM prompt is logged in
``iterations/iteration-NNN/spec/spec_gen_prompt.log``; feedback-only text is in
``iterations/iteration-NNN/bench/iteration_feedback.txt``.

**Experiment trajectory file**: each workspace maintains ``experiment_summary.md``
(append-only). After every spec generation it records deployment, diff vs the
previous iteration, and LLM rationale (text before ``<SPEC>`` in ``spec_gen.log``).
After every Locust run it records time range, an adaptive ramp table (from
``bench.log``), aggregate req/fail stats, and top errors.

Standalone spec-only (no deploy): use ``--mode k8s-bench`` with ``--k8s-iteration``
to pin a single phase, or edit ``spec.yaml`` by hand and re-bench with
``--deploy-only``.
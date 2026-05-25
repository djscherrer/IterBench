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

1. **LLM spec generation** → `k8s_configs/iteration-NNN/spec.yaml`
2. **Render** Deployment/Service manifests
3. **Deploy** + **Locust** benchmark
4. **Feedback** written to `perf-k8s-…/iteration_feedback.json` for the next phase

**Experiment slug** (optional): set `K8S_EXPERIMENT` / `--k8s-experiment` /
`BAXBENCH_K8S_EXPERIMENT` to group configs and perf runs under
`sampleN/k8s-experiments/<slug>/` (iterations still `iteration-001`, …).
Omit the slug for the legacy layout directly under `sampleN/`. A new slug starts
a fresh chain without skipping phases from an older experiment.

**Namespace cleanup** (always on): before each deploy and after each bench run,
all `baxbench-*` Kubernetes namespaces are deleted automatically (frees cluster
CPU; results on disk are kept). Set `BAXBENCH_K8S_CLEANUP=false` only to disable.

**Spec validation**: LLM specs are checked against **per-worker** capacity (each
pod must fit on one node using **requests**), connection pool budget
(`replicas × pool_max ≤ database.max_connections`), and cluster request totals.
Failed specs are re-prompted to the LLM (up to 3 attempts) with error details
before deploy.

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

After **generate** + **test**:

```bash
./scripts/bench_k8s.sh
# or: --k8s-iterations 3  for iteration-001 .. iteration-003
```

### CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--k8s-iterations N` | 1 | Phases `iteration-001` … `iteration-NNN` |
| `--k8s-spec-gen` / `--no-k8s-spec-gen` | on | LLM specs vs deploy-only |
| `--k8s-iteration iteration-001` | — | Pin a single phase (ignores N) |
| `--k8s-experiment adaptive-may20` | — | Workspace under `k8s-experiments/<slug>/` |
| `--force` | — | Regenerate specs and re-bench |

Phase 1 prompt: scenario, framework, `high_performance`, app code excerpt, cluster capacity.

Phase 2+ prompt adds **feedback**: Locust per-endpoint stats as a markdown table
(from ``bench_results_*_stats.csv``), Locust top errors, Kubernetes pod/node
utilization aggregated over the run (min/avg/max from ``stats/kubernetes/*.csv``),
previous ``spec.yaml``. The full LLM prompt is logged in
``k8s_configs/<iteration>/spec_gen_prompt.log``; feedback-only text is in
``perf-.../iteration_feedback.txt``.

**Experiment trajectory file**: each workspace maintains ``experiment_summary.md``
(append-only). After every spec generation it records deployment, diff vs the
previous iteration, and LLM rationale (text before ``<SPEC>`` in ``spec_gen.log``).
After every Locust run it records time range, an adaptive ramp table (from
``bench.log``), aggregate req/fail stats, and top errors. Disable with
``BAXBENCH_K8S_EXPERIMENT_SUMMARY=false``.

Standalone spec-only (no deploy): `--mode k8s-spec-gen` or `./scripts/generate_k8s_spec.sh`.
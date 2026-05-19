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
| `--force` | — | Regenerate specs and re-bench |

Phase 1 prompt: scenario, framework, `high_performance`, app code excerpt, cluster capacity.

Phase 2+ prompt adds **feedback**: Locust aggregated stats, error report excerpt,
`kubectl top pods`, previous `spec.yaml`.

Standalone spec-only (no deploy): `--mode k8s-spec-gen` or `./scripts/generate_k8s_spec.sh`.
# Phase: Load Benchmark (Locust)

Corresponds to `05-bench/`.

---

## Role

Measure **runtime performance** of the deployed application under realistic HTTP
load, using the same scenario workloads BaxBench defines for functional testing.

---

## Load generation architecture

- **Distributed Locust** — master coordinates, workers generate requests.
- Runs on dedicated load hosts (see [02-system-architecture.md](02-system-architecture.md)).
- Targets the Kubernetes **Service** fronting backend pods.

---

## Load profile

**See [13-load-profile-and-goodput.md](13-load-profile-and-goodput.md) for depth guidance** (what goes in Methods vs. appendix).

Summary for this section:

- Default profile: **`k8s-goodput-plateau`** (see `scripts/bench_k8s.sh`).
- **Adaptive ramp** — step-wise user increases until goodput plateaus; backs off on fail%, high p95, or goodput collapse.
- **Goodput** — successful RPS only; primary cross-iteration metric (sustained goodput from rolling window).
- Profile manifest staged as `baxbench_load_profile.json`; resolved values also in `05-bench/config.json`.

Reference: `locust_bench/load_profiles/registry.py`, `docs/locust_pipeline.md`.

---

## Metrics collected

### From Locust

| Metric | Use |
|--------|-----|
| Requests/s per endpoint | Throughput breakdown |
| p50 / p95 latency | SLA analysis |
| Failure count / rate | Stability |
| Adaptive ramp table | Goodput plateau visualization |

### From diagnostics (`bench_diagnostics/`)

| Source | Use |
|--------|-----|
| `kubectl top` pods/nodes | CPU/memory during run |
| Pod logs (backend, postgres) | Errors, OOM, connection issues |
| `pg_stat_*` | DB bottlenecks |
| Locust host metrics | Load generator saturation |

Diagnostics can be disabled (`BAXBENCH_K8S_DIAGNOSTICS=0`) — state what you used.

---

## Run directory layout

```text
05-bench/
├── bench.log
├── config.json
├── locust/results/*.csv
├── diagnostics/kubernetes/...
└── plots/   (if generated post-hoc)
```

---

## Thesis bullets

- Explain **why adaptive ramp** vs. fixed RPS (finds capacity knee without manual tuning per app).
- State SLA definition used (e.g. p95 < X ms, error rate < Y%).
- Note bench runs **after** namespace deploy; cleanup after bench frees resources for next iteration.
- Mention wall-clock duration drivers: `run_time_s`, ramp steps, cluster size.

---

## Link to Results chapter

- Plot type: goodput per iteration (`plots/goodput_per_iteration.png`).
- Table: iteration × replicas × CPU request × measured goodput × p95.

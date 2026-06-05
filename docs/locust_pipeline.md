## BaxBench Locust pipeline

All Locust code is under **`src/locust_bench/`**. Bench observability is in
the top-level **`src/bench_diagnostics/`** package (not under Locust).

| Module | When it runs |
|--------|--------------|
| **`locust_run.py`** — `DistributedLocustSession`, `LocustRunner` | **SSH load hosts** (k8s-bench + `distributed_bench`) |
| **`tasks.py`** — `run_bench_with_timeout` | **Local docker** `bench` mode only (Locust on this machine → `localhost`) |
| **`bench_diagnostics/`** | Per-run host / pod / cluster / database diagnostics |

Deploy/orchestration: **`distributed_bench/`** (SSH + Docker) or **`k8s_bench/`** (Kubernetes).

### Per-run on-disk layout

Each bench run produces a single self-contained directory (the iteration
``05-bench/`` folder for k8s-bench, or ``<sample>/perf-…/`` for the other
modes):

```
<run_dir>/
├── bench.log
├── config.json
├── iteration_feedback.{json,txt}
├── locust/                              # locust_bench.paths
│   ├── locustfile-<scenario>.py
│   ├── _baxbench_shape.py
│   ├── results/<test>_*.csv
│   └── logs/master-*.log, worker-*.log
└── diagnostics/                         # bench_diagnostics.paths
    ├── hosts/                           # shared: Locust SSH load generators
    │   └── <host_slug>/host_performance.csv
    ├── kubernetes/                      # k8s-bench only (never created in distributed mode)
    │   ├── cluster/kubectl_top_*.csv, pod_status.csv, events.jsonl, restart_logs/
    │   ├── pods/backend.log, postgres.log
    │   └── database/pg_stat_*.csv
    └── distributed/                     # distributed_bench only (never created in k8s mode)
        ├── hosts/<host_slug>/host_performance.csv, socket_queue.csv, …
        └── database/db_performance.csv  # local docker bench
```

Only the subtree for the active mode is created. Pass
:class:`bench_diagnostics.DiagnosticsMode` to
:func:`bench_diagnostics.diagnostics_session` (or the convenience wrappers
``diagnostics_session_for_k8s`` / ``diagnostics_session_for_distributed``).

Locust paths: ``locust_bench.paths.locust_csv_prefix(run_dir, test)``.

### Diagnostics collectors (`bench_diagnostics/`)

| Collector | Mode | Output |
|-----------|------|--------|
| ``LoadHostMetricsCollector`` | both | ``diagnostics/hosts/<host>/host_performance.csv`` |
| ``ClusterDiagnostics`` | kubernetes | ``diagnostics/kubernetes/cluster/…`` |
| ``PodLogsCollector`` | kubernetes | ``diagnostics/kubernetes/pods/…`` |
| ``PostgresMetricsCollector`` | kubernetes | ``diagnostics/kubernetes/database/…`` |
| ``WorkloadHostMetricsCollector`` | distributed | ``diagnostics/distributed/hosts/<host>/…`` |

Pod-log streaming + ``pg_stat_*`` on k8s can be disabled with
``BAXBENCH_K8S_DIAGNOSTICS=0``.

### Load profiles and `BAXBENCH_*` env vars

Profiles are defined in `locust_bench/load_profiles/registry.py` (e.g. `quick-check`, `stairs-800-100-30-12`).

At run time, `load_profiles/env.py` sets environment variables read by `_baxbench_shape.py`:

| Mode (`BAXBENCH_LOAD_MODE`) | Extra variables | Meaning |
|----------------------------|-----------------|---------|
| `steady` | `BAXBENCH_STEADY_USERS` | Fixed user count for the whole run |
| `continuous` | `CONTINUOUS_SPAWN_RATE`, `START_USERS`, `TARGET_USERS` | Ramp users linearly |
| `stairs` | `STAIRS_START_USERS`, `STEP_USERS`, `STEP_DURATION_S`, `STEPS` | Stepwise increase |
| `spike` | `SPIKE_BASE_USERS`, `SPIKE_USERS`, `INTERVAL_S`, `DURATION_S` | Periodic spikes |
| `adaptive` | `ADAPTIVE_SLA_MS`, `MAX_USERS`, `TRIM_S`, … | Adjust users from latency |

Common to all modes: `BAXBENCH_RUN_TIME_S`, `BAXBENCH_LOCUST_WAIT_MIN_S` / `MAX_S`.

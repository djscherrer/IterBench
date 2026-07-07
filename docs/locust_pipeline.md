## BaxBench Locust pipeline

All Locust code is under **`src/locust_bench/`**. Bench observability is in
the top-level **`src/bench_diagnostics/`** package (not under Locust).

| Module | When it runs |
|--------|--------------|
| **`locust_run.py`** — `DistributedLocustSession`, `LocustRunner` | **SSH load hosts** (k8s-bench + `distributed_bench`) |
| **`tasks.py`** — `run_bench_with_timeout` | **Local docker** `bench` mode only (Locust on this machine → `localhost`) |
| **`bench_diagnostics/`** | Per-run host / pod / cluster / database diagnostics |

Deploy/orchestration: **`distributed_bench/`** (SSH + Docker) or **`k8s_bench/`** (Kubernetes).

### K8s bench: probe → Locust

For k8s-bench, the **04-deploy** stage writes `probe.json` (namespace, image, port, NodePort target). The **05-bench** stage reads that probe and runs `run_distributed_locust` in `k8s_bench/stages/bench.py` — distributed Locust on SSH load hosts plus `diagnostics_session_for_k8s`. Bench does not re-resolve NodePort or patch `spec.yaml`; missing probe runtime fields fail with “re-run deploy”.

`config.json` under `05-bench/` records the LLM `spec.yaml` snapshot and the full `deploy_result` (including runtime fields) for plotting and inspection.

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

### Load profiles and manifest

Profiles are defined in `locust_bench/load_profiles/registry.py` (e.g. `quick-check`, `k8s-goodput-plateau`).

At staging time, `prepare_locust_run_dir()` writes `baxbench_load_profile.json` next to the locustfile. The manifest is a JSON snapshot of the resolved profile dataclass (mode, `run_time_s`, wait times, and all shape parameters). `_baxbench_shape.py` reads this file at runtime — no per-parameter environment variables.

The same manifest is embedded in `05-bench/config.json` under `resolved_load_profile` for post-run inspection and plotting.

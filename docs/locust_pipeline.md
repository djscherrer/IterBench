## BaxBench load-test pipeline

Load generation and profiling live in **`src/load_bench/`** (Locust runner, shapes,
manifests). That is separate from **`src/k8s_bench/`**, which handles Kubernetes
deploy and experiment orchestration. Bench observability is in the top-level
**`src/bench_diagnostics/`** package.

| Module | When it runs |
|--------|--------------|
| **`locust_run.py`** — `DistributedLocustSession` | **SSH load hosts** (k8s-bench) |
| **`bench_diagnostics/`** | Per-run host / pod / cluster / database diagnostics |

### K8s bench: probe → Locust

For k8s-bench, the **04-deploy** stage writes `probe.json` (namespace, image, port, NodePort target). The **05-bench** stage reads that probe and runs `run_distributed_locust` in `k8s_bench/stages/bench.py` — distributed Locust on SSH load hosts plus `diagnostics_session_for_k8s`. Bench does not re-resolve NodePort or patch `spec.yaml`; missing probe runtime fields fail with “re-run deploy”.

`config.json` under `05-bench/` records the LLM `spec.yaml` snapshot and the full `deploy_result` (including runtime fields) for plotting and inspection.

### Per-run on-disk layout

Each bench run produces a single self-contained directory (the iteration
``05-bench/`` folder for k8s-bench):

```
<run_dir>/
├── bench.log
├── config.json
├── iteration_feedback.{json,txt}
├── locust/                              # load_bench.paths
│   ├── locustfile-<scenario>.py
│   ├── _baxbench_shape.py               # facade (import BaxbenchShape)
│   ├── shapes/                          # shape implementations + helpers
│   ├── baxbench_load_profile.json
│   ├── results/<test>_*.csv
│   └── logs/master-*.log, worker-*.log
└── diagnostics/                         # bench_diagnostics.paths
    ├── hosts/                           # Locust SSH load generators
    │   └── <host_slug>/host_performance.csv
    └── kubernetes/
        ├── cluster/kubectl_top_*.csv, pod_status.csv, events.jsonl, restart_logs/
        ├── pods/backend.log, postgres.log
        └── database/pg_stat_*.csv
```

Start collectors via :func:`bench_diagnostics.diagnostics_session_for_k8s`.

Locust paths: ``load_bench.paths.locust_csv_prefix(run_dir, test)``.

### Diagnostics collectors (`bench_diagnostics/`)

| Collector | Output |
|-----------|--------|
| ``LoadHostMetricsCollector`` | ``diagnostics/hosts/<host>/host_performance.csv`` |
| ``ClusterDiagnostics`` | ``diagnostics/kubernetes/cluster/…`` |
| ``PodLogsCollector`` | ``diagnostics/kubernetes/pods/…`` |
| ``PostgresMetricsCollector`` | ``diagnostics/kubernetes/database/…`` |

Pod-log streaming + ``pg_stat_*`` on k8s can be disabled with
``BAXBENCH_K8S_DIAGNOSTICS=0``.

### Load profiles and manifest

Profiles are defined in `load_bench/load_profiles/registry.py` (e.g. `quick-check`, `k8s-goodput-plateau`).

At staging time, `prepare_locust_run_dir()` writes `baxbench_load_profile.json` next to the locustfile and copies `_baxbench_shape.py` plus the `shapes/` package. The manifest is a JSON snapshot of the resolved profile dataclass (mode, `run_time_s`, wait times, and all shape parameters). The shape package reads this file at runtime — no per-parameter environment variables.

The same manifest is embedded in `05-bench/config.json` under `resolved_load_profile` for post-run inspection and plotting.

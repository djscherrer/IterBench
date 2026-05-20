## BaxBench Locust pipeline

All Locust code is under **`src/locust_bench/`**.

| Module | When it runs |
|--------|----------------|
| **`locust_run.py`** — `DistributedLocustSession`, `LocustRunner` | **SSH load hosts** (k8s-bench + `distributed_bench`) |
| **`tasks.py`** — `run_bench_with_timeout` | **Local docker** `bench` mode only (Locust on this machine → `localhost`) |
| **`utilization_logging/`** | Per-run metrics under ``stats/`` (see below) |

Deploy/orchestration: **`distributed_bench/`** (SSH + Docker) or **`k8s_bench/`** (Kubernetes).

### Utilization logging (`utilization_logging/`)

Written under each perf run directory (``stats/``):

| Logger | Used by | Output |
|--------|---------|--------|
| ``LoadHostUtilizationLogger`` | k8s-bench + distributed bench | ``stats/<load-host>/host_performance.csv`` |
| ``DistributedBenchUtilizationLogger`` | distributed bench only | ``stats/<app-host>/host_performance.csv``, ``socket_queue.csv`` |
| ``KubernetesUtilizationLogger`` | k8s-bench only | ``stats/kubernetes/pod_top.csv``, ``node_top.csv`` |

Kubernetes CSV columns (from ``kubectl top --no-headers``):

- **pod_top**: ``ts_epoch_s``, ``ts``, ``pod``, ``cpu`` (millicores, e.g. ``80m``), ``memory`` (e.g. ``809Mi``)
- **node_top**: ``ts_epoch_s``, ``ts``, ``node``, ``cpu`` (millicores), ``cpu_pct``, ``memory`` (e.g. ``15341Mi``), ``memory_pct``

**Load-host logging** runs in every mode. **K8s vs distributed workload logging** is mutually exclusive — use ``utilization_session_for_k8s`` or ``utilization_session_for_distributed`` (see ``locust_run.LocustRunner``, ``k8s_bench/iteration.py``).

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

### Data produced

Locust CSVs land under the sample perf run directory; distributed runs also copy worker logs from SSH hosts.

## BaxBench Locust pipeline

All Locust code is under **`src/locust_bench/`**.

| Module | When it runs |
|--------|----------------|
| **`runner.py`** — `LocustRunner` | **Remote** load hosts over SSH (`distributed_bench` orchestrator) |
| **`local_runner.py`** — `run_headless_locust` | **This machine** (`k8s-bench` via port-forward, local docker bench) |

Deploy/orchestration: **`distributed_bench/`** (SSH + Docker) or **`k8s_bench/`** (Kubernetes).

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

These are **not removed** — they were moved from inline code in the old `locustrunner.py` into `load_profiles/env.py` so local and remote runners share one definition.

### Data produced

See per-run `perf-*` / `perf-k8s-*` directories: Locust CSVs, `bench.log`, and (remote bench only) `stats/<host>/` telemetry collected alongside `LocustRunner.run`.

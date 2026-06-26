# Distributed benchmark scripts

Shell wrappers for the **non-iterative** BaxBench pipeline: generate code, run
functional tests, deploy to SSH/Docker hosts, and run distributed Locust benches.

Run from anywhere; each script `cd`s to the repo root automatically.

| Script | Purpose |
|--------|---------|
| `preflight.sh` | Validate remote hosts, Docker, and load-generator connectivity (`--mode preflight`) |
| `generate.sh` | LLM code generation (`--mode generate`) |
| `test.sh` | Functional / security tests (`--mode test`) |
| `bench.sh` | Distributed Locust benchmark (`--mode bench`) |
| `evaluate.sh` | Print pass@k tables (`--mode evaluate`) |
| `plot.sh` | Plot perf run directories (`--mode plot`) |
| `run_perf_suite.sh` | Configurable multi-mode pipeline (generate → test → bench → …) |
| `remote_cleanup.sh` | Kill BaxBench SSH tunnels and remove `baxbench-*` containers on hosts |
| `remote_docker_prune.sh` | Aggressive remote Docker prune across hosts |
| `cleanup_docker_data.sh` | Remove local image `.tar` files + remote Docker cleanup |

Kubernetes iterative benchmarking lives in `scripts/` (`bench_k8s.sh`, `k8s_*.sh`).

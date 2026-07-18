<div align="center">
    <h1>BaxBench — Kubernetes Deployment Optimization Fork</h1>
</div>

## Overview

This repository is a fork of [BaxBench](https://baxbench.com) ([paper](https://arxiv.org/abs/2502.11844)), a benchmark that evaluates whether LLMs can generate correct and secure backend applications. The fork keeps BaxBench's scenario/framework/task definitions and builds an iterative deployment-optimization loop on top, used for a master's thesis on LLM-driven deployment optimization under fixed cluster resource constraints.

**`k8s_bench`** is the core contribution: an iterative loop (decide → code → spec → deploy → benchmark → feedback) where an LLM agent repeatedly refines both the *application code* and its *Kubernetes deployment configuration*, guided by real Locust load-test goodput and cluster resource utilization. See [docs/k8s_approach.md](docs/k8s_approach.md).

## Repository layout

```
src/
├── main.py               # CLI entrypoint (--mode k8s-bench / k8s-preflight / k8s-setup-cluster / k8s-setup-registry / k8s-plot)
├── tasks.py              # Task, TaskHandler — scenario/framework/model task bookkeeping
├── bench_models.py       # shared config models
├── remote_exec.py        # SSH/remote command execution helper
│
├── scenarios/            # BaxBench task definitions (API + functional/security tests per scenario)
├── env/                  # BaxBench framework/language environments (Docker templates per framework)
├── scenario_files/       # Locust files + OpenAPI specs consumed by scenarios/ and k8s_bench
│
├── llm/                  # provider-agnostic LLM client: prompting, caching, conversation state, providers/
├── k8s_bench/            # iterative deployment-optimization loop (cluster/, code/, spec/, stages/, plots/, ...)
├── load_bench/           # Locust runner, load shapes/profiles, goodput measurement
└── bench_diagnostics/    # live diagnostics collection (k8s pods, DB, remote Locust hosts) + run-directory summary parsing

scripts/
├── bench_k8s.sh, k8s_preflight.sh, k8s_setup_cluster.sh, k8s_run_iteration.sh
├── analysis/             # results aggregation across models/scenarios
└── results_overview.py, fetch_results.sh

docs/                     # design notes for the k8s pipeline (approach, prompt design, failure taxonomy, Locust pipeline)
results/                  # generated code + logs, one dir per model (gitignored)
results_aggregate/        # cross-run aggregate tables/figures (gitignored)
```

## Installation

**Prerequisites:**

- `python 3.12`
- `docker`, with root/daemon privileges on your machine
- `pipenv` for package management

Install the environment from the repo root:

```bash
pipenv sync
```

Run any script inside the project environment:

```bash
pipenv run python <path_to_script> <args>
```

**API keys**

Copy `.env.example` to `.env` (or export these directly) and fill in the keys you intend to use — any key you don't need can be left empty:

```bash
OPENAI_API_KEY=
TOGETHER_API_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=
CSCS_API_KEY=
```

## Usage

Everything is driven through `src/main.py --mode <mode>`; `scripts/` just wraps common invocations.

```bash
scripts/k8s_preflight.sh        # validate cluster access
scripts/k8s_setup_cluster.sh    # provision the benchmarking cluster
scripts/bench_k8s.sh            # run the iterative decide/code/spec/deploy/bench loop
```

Restrict the task set with `--scenarios`, `--envs`, `--only_samples` (space-separated values). Arguments can also be loaded from a file, e.g. `python src/main.py @config.args`.

See [docs/k8s_approach.md](docs/k8s_approach.md), [docs/k8s_stage_failures.md](docs/k8s_stage_failures.md), [docs/k8s_conversational_prompt_slimming.md](docs/k8s_conversational_prompt_slimming.md), and [docs/locust_pipeline.md](docs/locust_pipeline.md) for design details.

#### Aggregating results

```bash
pipenv run python scripts/analysis/aggregate_evaluation.py
pipenv run python scripts/results_overview.py
```

## Troubleshooting

Check the k8s-bench iteration logs for token-limit/rate-limit/provider errors; `docs/k8s_stage_failures.md` has the failure taxonomy per stage.

## Citation

This repository builds on the original BaxBench benchmark:

```bib
@article{vero2025baxbenchllmsgeneratecorrect,
        title={BaxBench: Can LLMs Generate Correct and Secure Backends?},
        author={Mark Vero and Niels Mündler and Victor Chibotaru and Veselin Raychev and Maximilian Baader and Nikola Jovanović and Jingxuan He and Martin Vechev},
        year={2025},
        eprint={2502.11844},
        archivePrefix={arXiv},
}
```

## License

MIT. Check `LICENSE`.

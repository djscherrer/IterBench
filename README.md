<div align="center">
    <img src="docs/img/mascot.png" alt="IterBench mascot" width="200">
    <h1>IterBench — Kubernetes Deployment Optimization Fork of BaxBench</h1>
</div>

## Overview

**IterBench** is this repository's framework name in the thesis; the code identifiers throughout the repo (module names, CLI flags, directory names) stay `baxbench-*`/`k8s_bench` for continuity with the fork it's built on. This repository is a fork of [BaxBench](https://baxbench.com) ([paper](https://arxiv.org/abs/2502.11844)), a benchmark that evaluates whether LLMs can generate correct and secure backend applications. The fork keeps BaxBench's scenario/framework/task definitions and builds an iterative deployment-optimization loop on top, used for a master's thesis on LLM-driven deployment optimization under fixed cluster resource constraints.

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
│   └── generated_scenarios/  # AutoBaxBuilder output, pending manual review (see below)
├── env/                  # BaxBench framework/language environments (Docker templates per framework)
├── scenario_files/       # Locust files + OpenAPI specs consumed by scenarios/ and k8s_bench
│
├── llm/                  # provider-agnostic LLM client: prompting, caching, conversation state, providers/
├── workspace/            # k8s-bench experiment workspace: iteration-directory layout, decision/feedback artifacts,
│                         #   iteration metadata (shared by k8s_bench and plots/)
├── k8s_bench/            # iterative deployment-optimization loop (cluster/, code/, spec/, stages/, reverify/, ...)
├── plots/                # all plotting/reporting: per-experiment goodput + ramp plots, aggregate/ (cross-experiment
│                         #   tables + figures), ramp/ (adaptive-ramp data + plot), reporting/ (pass@k text tables)
├── load_bench/           # Locust runner, load shapes/profiles, goodput measurement
├── bench_diagnostics/    # live diagnostics collection (k8s pods, DB, remote Locust hosts) + run-directory summary parsing
└── scenario_builder/     # AutoBaxBuilder: bootstraps new scenarios (see "Generating new scenarios" below).
                          # Its own package, but shares this repo's env/, scenarios/, llm/, tasks.py, cwes.py via sys.path.

scripts/
├── bench_k8s.sh, k8s_preflight.sh, k8s_setup_cluster.sh, k8s_run_iteration.sh
├── k8s_rebench_results.py   # bulk deploy+bench reverification across a whole results tree (see docs/k8s_approach.md)
├── orchestrate_scenarios.sh  # drives scenario_builder to generate + export a new scenario
├── analysis/             # results aggregation across models/scenarios
└── results_overview.py, fetch_results.sh

tests/                    # pytest unit tests (pure-logic modules only, e.g. k8s_bench/reverify/); `pytest` from repo root
docs/                     # design notes for the k8s pipeline (approach, prompt design, failure taxonomy, Locust pipeline)
results/                  # generated code + logs, one dir per model (gitignored)
results_reverified/       # deploy-only repeated measurements (gitignored)
results_reverified2/      # second reverification pass, gap-fills what results_reverified/ missed (gitignored)
results_aggregate/        # cross-run aggregate tables/figures (gitignored)
gen_scenarios/            # scenario_builder's own artifacts/ + results/ (gitignored) — see
                          #   "Generating new scenarios" below and gen_scenarios/README.md
```

To stream a remote re-verification tree into an archive, use
`scripts/fetch_results.sh USER@HOST REMOTE_REPO LOCAL_OUTPUT_DIR
REMOTE_RESULTS_DIR`; the last argument defaults to `results_reverified`.
Extract the archive in the repository root so it creates the separate
`results_reverified/` tree alongside the original `results/`.

To compare the two trees after extraction (including sustained-goodput
deviation, peak-versus-sustained gaps, persisted NodePort targets, Locust
master-network evidence, and byte-identical rows that were not re-run), use:

```bash
.venv/bin/python scripts/analysis/load_profile_repeatability.py \
  --original-root results --reverified-root results_reverified \
  --out-dir results_aggregate/repeatability
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

Raw result trees are gitignored and too large to commit; trimmed reference
snapshots are published as GitHub Release assets instead — see
[docs/results_archive.md](docs/results_archive.md) for what's in them, the
network-confound background, and the download command.

## Generating new scenarios (AutoBaxBuilder)

`src/scenario_builder/` bootstraps new BaxBench scenarios end-to-end — idea → OpenAPI spec → reference solution → functional tests → security exploits → Locust script — cutting manual scenario-authoring effort by ~12× while matching or outperforming expert-written tests and exploits ([paper](https://arxiv.org/abs/2512.21132)). It's a separate package from the rest of this repo, but resolves `env`, `scenarios`, `llm`, `tasks`, and `cwes` to this repo's own copies rather than keeping forks.

```bash
scripts/orchestrate_scenarios.sh
```

or drive it directly (its entry point is `orchestrator.py`, run from `src/scenario_builder/`):

```bash
cd src/scenario_builder
python orchestrator.py --generate_scenarios
python orchestrator.py --generate_tests --scenario FooBarScenario
python orchestrator.py --generate_exploits --scenario FooBarScenario
python orchestrator.py --generate_performance --scenario FooBarScenario
python orchestrator.py --export_latest --scenario FooBarScenario
```

Each `--generate_*` step writes numbered artifacts into the artifacts directory (`FooBarScenario_iu{t}` after t test-iteration steps, `_iw{t}` after t security-iteration steps, `_implementations_i{t/u/w}{t}` for the corresponding solutions). `--export_latest` promotes the newest iteration into `src/scenarios/generated_scenarios/` — a staging area for manual review before a scenario is wired into `scenarios.all_scenarios`.

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

`src/scenario_builder/` builds on AutoBaxBuilder, used to bootstrap new scenarios:

```bib
@article{vonarx2025autobaxbuilderbootstrappingcodesecurity,
      title={AutoBaxBuilder: Bootstrapping Code Security Benchmarking},
      author={Tobias von Arx and Niels Mündler and Mark Vero and Maximilian Baader and Martin Vechev},
      year={2025},
      eprint={2512.21132},
      archivePrefix={arXiv},
}
```

## License

MIT. Check `LICENSE`.

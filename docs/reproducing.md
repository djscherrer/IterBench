# Running and reproducing

There are three useful levels of reproduction. Most readers can inspect or
re-render the published analysis from the tracked aggregate CSVs. Rebuilding
those CSVs needs the archived raw result trees. Running a new IterBench
experiment additionally needs a dedicated multi-host Kubernetes and Locust
environment.

## Prerequisites

- Python 3.12
- `pipenv`
- Docker for generated application images
- API credentials for any model you plan to run
- For a live experiment: `kubectl`, passwordless SSH among the configured
  hosts, and a Kubernetes/Locust topology configured in
  [`src/k8s_bench/cluster/profiles.py`](../src/k8s_bench/cluster/profiles.py)

Install the development environment from the repository root:

```bash
pipenv install --dev
cp .env.example .env
```

Fill in only the provider keys you need in `.env`. The current `Pipfile.lock`
needs refreshing before `pipenv sync` can be relied on, so `pipenv install
--dev` is the working installation command at this revision.

Run the unit tests with:

```bash
pipenv run pytest
```

## Reproduce the published analysis

The four aggregate CSV files are tracked in Git and directly support the
published summary statistics and result appendix; no cluster or raw archive is
needed.

```bash
pipenv run python scripts/analysis/rq_summary_stats.py
pipenv run python scripts/analysis/generate_results_appendix_markdown.py
```

The first command prints the geometric-mean first-versus-best and
final-versus-first summaries. The second regenerates
[`docs/results_appendix.md`](results_appendix.md) from the four CSVs.

To recreate the aggregate figures from the public data, use a writable
Matplotlib cache directory (particularly useful on headless machines):

```bash
MPLBACKEND=Agg MPLCONFIGDIR=/tmp/iterbench-mpl \
  pipenv run python scripts/analysis/plot_aggregate_from_csv.py \
  --aggregate-dir results_aggregate --out-dir /tmp/iterbench-figures

MPLBACKEND=Agg MPLCONFIGDIR=/tmp/iterbench-mpl \
  pipenv run python scripts/analysis/plot_trajectories_with_explore.py \
  --phases results_aggregate/load_profile_phases.csv \
  --iterations results_aggregate/iterations.csv \
  --failures results_aggregate/failures.csv \
  --out-dir /tmp/iterbench-figures
```

The tracked documentation images are rendered copies of the final dataset.
The commands above reproduce the figures from the same values; image bytes may
differ with Matplotlib, fonts, or metadata.

## Rebuild the aggregates from raw results

The original result trees are intentionally not committed because they contain
large logs and diagnostics. Their provenance and the network-consistent merge
used for the reported evaluation are described in
[`docs/results_archive.md`](results_archive.md). After obtaining the raw
trees and assembling the final `results/` tree, run:

```bash
MPLBACKEND=Agg MPLCONFIGDIR=/tmp/iterbench-mpl \
  pipenv run python scripts/analysis/aggregate_evaluation.py \
  --results-root results --experiment-slug results --out-dir results_aggregate

MPLBACKEND=Agg MPLCONFIGDIR=/tmp/iterbench-mpl \
  pipenv run python scripts/analysis/load_profile_phases.py \
  --results-root results --experiment-slug results \
  --out-csv results_aggregate/load_profile_phases.csv

pipenv run python scripts/analysis/generate_results_appendix_markdown.py
```

This regenerates the 63 task summaries, 632 benchmark rows, 61 pre-benchmark
failure rows, phase outcomes, aggregate figures, and the GitHub appendix.
The two raw-result reverification passes correct an earlier network-path
confound; use the final merged `results/` tree rather than analysing any one
archive in isolation.

## Run a new experiment

### 1. Define a cluster profile

Add or edit a profile in
[`src/k8s_bench/cluster/profiles.py`](../src/k8s_bench/cluster/profiles.py).
It must identify a control node, Kubernetes workers, a Locust master and
workers, and (when used) a private image registry. `lab-default` is a template,
not a runnable topology; create a profile for your own environment.

### 2. Prepare the cluster

From the control host, run the wrappers in this order:

```bash
BAXBENCH_K8S_CLUSTER=<your-profile> scripts/k8s_preflight.sh
BAXBENCH_K8S_CLUSTER=<your-profile> scripts/k8s_setup_cluster.sh
```

The preflight wrapper checks host prerequisites; set
`K8S_INSTALL_PREREQUISITES=true` only when you intend it to install them.
Cluster setup
initialises Kubernetes, configures the CNI, and sets up the registry when the
selected profile enables it.

### 3. Smoke test one baseline

Run a small, separate experiment slug before starting a full campaign:

```bash
pipenv run python src/main.py --mode k8s-bench \
  --models openai/gpt-5.5-2026-04-23 \
  --only_samples 0 --envs Python-Flask --scenarios ClickCount \
  --temperature 0.2 --safety_prompt high_performance \
  --k8s-cluster <your-profile> --k8s-experiment smoke \
  --k8s-iterations 0 --load-profile quick-check --llm-max-cost 2
```

`--k8s-iterations 0` means a baseline only. Use the
Explore-Refine profile for an evaluation-compatible trajectory.

### 4. Run a configured campaign

[`scripts/bench_k8s.sh`](../scripts/bench_k8s.sh) deliberately has no embedded
model, task, or cluster selection. Supply your own scope as environment
variables; this baseline-only example keeps a first live run bounded:

```bash
MODELS=<provider/model> ENVS=<framework> SCENARIOS=<scenario> \
  K8S_CLUSTER=<your-profile> K8S_EXPERIMENT=smoke \
  K8S_ITERATIONS=0 BAXBENCH_LLM_MAX_COST=5 \
  scripts/bench_k8s.sh
```

The final evaluation grid is recorded in [Evaluation](evaluation.md), not
used as an implicit default by an executable script.

The script's values are set inside the file, so its commented environment
variable examples are not runtime overrides. For a small custom run, edit that
block or use the direct CLI form shown above.

## Inspect, replot, or re-benchmark

Replot a single recorded experiment:

```bash
pipenv run python src/main.py --mode k8s-plot \
  --k8s-experiment-dir <results-path>/sample0/k8s-experiments/results
```

For a fresh measurement of existing artifacts, use
[`scripts/k8s_rebench_results.py`](../scripts/k8s_rebench_results.py) on a
separate output tree first with `--dry-run`. Do not overwrite the original
evaluation results when re-benchmarking.

## Scenario construction

The optional AutoBaxBuilder-derived scenario pipeline lives in
[`src/scenario_builder`](../src/scenario_builder). Its main phases are
`--generate_scenarios`, `--generate_tests`, `--generate_exploits`,
`--generate_performance`, and `--export_latest`. See the
[top-level README](../README.md#generating-new-scenarios-autobaxbuilder) for
the current commands and [Methods](methods.md#offline-scenario-and-workload-construction)
for what the resulting scenario package contributes to IterBench.

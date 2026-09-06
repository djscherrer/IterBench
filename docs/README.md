# IterBench documentation

IterBench is the Kubernetes deployment-optimisation extension of
[BaxBench](https://arxiv.org/abs/2502.11844). It evaluates an LLM agent that
generates a backend, deploys it under fixed cluster constraints, measures it
under load, and iteratively refines either code or deployment configuration.
The repository retains `baxbench-*` and `k8s_bench` identifiers for continuity
with the upstream fork.

These pages are a compact, repository-oriented companion to the thesis. They
describe the final evaluation dataset checked into this repository; the CSV
files are the source of truth for reported values.

The complete thesis is available as [a PDF](Scherrer_David_Masters_Thesis_647.pdf).

| Start here | What it covers |
| --- | --- |
| [Background and scope](background.md) | What BaxBench, AutoBaxBuilder, and IterBench each contribute. |
| [Methods](methods.md) | Scenario construction, the iterative benchmark, deployment search space, and adaptive load profile. |
| [Running and reproducing](reproducing.md) | Rebuild the published summaries, regenerate analysis from archived results, or run a new cluster experiment. |
| [Evaluation](evaluation.md) | Experimental setup, measurement checks, results, and limitations. |
| [Complete results appendix](results_appendix.md) | Every baseline and refinement outcome for all 63 tasks. |

## Data and provenance

The four tracked aggregate files are sufficient to inspect the reported
evaluation without downloading the multi-gigabyte result trees:

| File | Contents |
| --- | --- |
| [`cells.csv`](../results_aggregate/cells.csv) | One summary row for each of the 63 model × scenario × framework tasks. |
| [`iterations.csv`](../results_aggregate/iterations.csv) | The 632 candidates that reached the load-test stage. |
| [`failures.csv`](../results_aggregate/failures.csv) | The 61 candidates that stopped before benchmarking. |
| [`load_profile_phases.csv`](../results_aggregate/load_profile_phases.csv) | The adaptive load-profile outcome for each completed bench run. |

Together, `iterations.csv` and `failures.csv` account for all 693 planned
candidates: a baseline plus ten refinement iterations for each task. See
[the reproduction guide](reproducing.md#reproduce-the-published-analysis)
for the exact commands and [the appendix](results_appendix.md) for a
human-readable grid.

The public CSVs are outcome tables, not raw experiment directories. Goodput
and duration values are rounded to 0.1, percentage changes to 0.01, and
task-level gain ratios to 0.001. In particular, `iterations.csv` retains the
iteration identity, selected refinement kind, and resulting goodput; it does
not present a partial subset of Kubernetes configuration fields as a complete
deployment record.

## Implementation notes

The pages above are reader-facing documentation. The following notes remain
useful when changing the implementation:

- [Kubernetes pipeline and artifact layout](k8s_approach.md)
- [Locust/load-generation implementation](locust_pipeline.md)
- [Stage-failure taxonomy](k8s_stage_failures.md)
- [Prompt-context design](k8s_conversational_prompt_slimming.md)
- [Archived raw-result provenance](results_archive.md)

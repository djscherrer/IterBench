# Result archives (GitHub Release)

Trimmed, downloadable snapshots of the evaluation result trees, published as
assets on the GitHub Release
[`results-archive-2026-08-22`](https://github.com/djscherrer/iterperfbench/releases/tag/results-archive-2026-08-22)
rather than committed into the repo (multi-hundred-MB files don't belong in
git history). None of these is the tree the analysis pipeline reads from —
that's the (gitignored, local-only) `results/` + `results_aggregate/` in the
working repo. These archives exist so the underlying data can be browsed
without pulling the full raw trees off the benchmarking machine.

```bash
gh release download results-archive-2026-08-22 --repo djscherrer/iterperfbench
```

## The four archives

- **`results_first_run_trimmed.tar.gz`** — the original, unmodified campaign
  archive.
- **`results_network_fix_and_reverification_trimmed.tar.gz`** — the first
  internal-LAN reverification pass.
- **`results_reverification2_trimmed.tar.gz`** — a second pass that gap-fills
  what the first one missed.
- **`gen_scenarios.tar.gz`** — the AutoBaxBuilder scenario-authoring working
  directory (untrimmed — see [`gen_scenarios/README.md`](../gen_scenarios/README.md)
  for what's in it; that file is tracked in the repo despite `gen_scenarios/`
  itself being gitignored).

The three `results_*` archives have their `05-bench/diagnostics/` (k8s pod
logs, pgbouncer/postgres logs, cluster metrics) and `05-bench/locust/logs/`
(raw per-worker Locust logs) subtrees stripped at archive-build time — that
was ~90% of the raw size and is pure operational debug telemetry, not
measured results. Everything that reflects an actual outcome (specs, code,
decisions, test results, deploy manifests, Locust stats CSVs, goodput/ramp
plots, cost ledgers) is intact. The untrimmed originals remain on the
benchmarking machine.

## The problem: a control-network confound

Benchmarks are driven by Locust against each candidate's Kubernetes service.
For part of the campaign, that load path partly crossed the shared,
lower-bandwidth Emulab control network instead of staying on the experiment
LAN, because the Locust NodePort and the k8s/Flannel routing weren't fully
pinned to the LAN yet. Two fixes landed during data collection:

- **2026-07-15 06:52** — Locust NodePort fix
- **2026-07-17 02:00** — k8s/Flannel pinning

Any cell with at least one iteration benchmarked before the second cutover
has a goodput measurement that is not comparable to the rest of the dataset:
it was throttled by a network hop the other measurements never crossed.
Classifying each cell by the timestamp of its original `bench.log` found
**17 of the 63 cells** (model x scenario x framework) affected: 11 gpt-5.5
cells and 6 glm-5.2 cells. Every claude-opus-4-8 cell was already measured
post-fix. The affected cells:

```
openai-gpt-5.5-2026-04-23/BranchWeave_InteractiveStoryGraph/{Go-net-http,Python-Flask,Rust-Actix}
openai-gpt-5.5-2026-04-23/ClickCount/{Go-net-http,Python-Flask,Rust-Actix}
openai-gpt-5.5-2026-04-23/Petstore/{Go-net-http,Python-Flask,Rust-Actix}
openai-gpt-5.5-2026-04-23/Recipes/{Go-net-http,Python-Flask}
z-ai-glm-5.2/BranchWeave_InteractiveStoryGraph/{Go-net-http,Python-Flask}
z-ai-glm-5.2/ClickCount/{Go-net-http,Python-Flask}
z-ai-glm-5.2/Petstore/Go-net-http
z-ai-glm-5.2/Recipes/Go-net-http
```

We fixed the network path and then re-benchmarked every candidate in those
17 cells entirely on the experiment LAN, twice (see below), so the final
dataset is internal-network-consistent throughout.

## What's in each archive

### `results_first_run_trimmed.tar.gz`
The original, unmodified campaign archive. Every cell was benchmarked here
first; for the 17 cells above, the `05-bench/` measurements in this tree are
the confounded, partly-control-network ones. `01-decision/02-code/03-spec`
and (by default) `04-deploy` are unaffected by the confound and are always
correct here, since the network path only touches the load-generation stage.
This archive also still contains a `TimeCapsuleNotesVault` scenario for
gpt-5.5 and glm-5.2: that scenario was dropped from the final 7-scenario set
before the anthropic runs started, so it never got copied into the repo's
`results/`. Its presence here is leftover, not a gap in the working tree.

### `results_network_fix_and_reverification_trimmed.tar.gz`
The first re-verification pass: every candidate in the 17 confounded cells
re-deployed and re-benchmarked from scratch, entirely on the experiment LAN,
after both fixes above landed. This is what the working repo's `results/`
borrows `05-bench/` from for those 17 cells (cell granularity: if any
iteration in a cell was confounded, the whole cell's bench data is swapped,
so a trajectory is never spliced from two measurement campaigns).

This pass was **incomplete**: it left 10 iterations unfilled across exactly
3 cells (2 gpt-5.5 Rust-Actix cells, 1 glm-5.2 ClickCount cell), and one
gpt-5.5 Petstore/Rust-Actix cell looked like it had crashed
(`locust_infra` -> 0 rps). That crash turned out to be a mistyped
`--load-profile` value (`explore-refine`/`explore_refine` instead of
`k8s-explore-refine`) that aborted 72+ Rust-Actix iterations across 15 cells
before Locust ever ran, not a real measurement problem.

### `results_reverification2_trimmed.tar.gz`
A second internal-LAN re-bench that gap-fills exactly what the first pass
missed: the 10 unfilled iterations in the 3 incomplete cells, plus a clean
re-bench of the previously-mistyped-load-profile Petstore/Rust-Actix cell
(after `scripts/cleanup/cleanup_wrong_load_profile_runs.py` cleared the
crashed iterations). That cell now measures ~27294 rps internal-LAN, in line
with the ~27853 rps it showed on the control network, so no
network-inconsistency caveat remains anywhere in the dataset.

## How they fit together into the working repo's `results/`

The gitignored `results/` tree that the analysis pipeline actually reads is
**not** a copy of `results_first_run/`. It started as one, then was patched
in place:

1. For the 17 confounded cells, `05-bench/` was replaced with the matching
   data from `results_network_fix_and_reverification/`.
2. For the 3 cells that pass left incomplete, the missing iterations were
   filled from `results_reverification2/`.
3. `cleanup_wrong_load_profile_runs.py` was run to clear and flag the
   mistyped-`--load-profile` crashes so the affected iterations show
   `failure_kind: "bench"` with an honest `failure_reason` instead of a raw
   crash trace.

So the working repo's `results/` is the single internal-network-consistent
ground truth; `results_aggregate/` and every figure/table in the thesis are
built from it. The archives on this release are the raw inputs to that
merge, kept for provenance and reproducibility, not for direct analysis.

## Verification (2026-08-21)

Before trimming and publishing, the local repo copies were checked against
the source directories on the benchmarking machine with `rsync` (dry-run,
`--delete`, size+mtime comparison over SSH):

- **`results_network_fix_and_reverification/`** and **`results_reverification2/`**
  were byte-identical to the local `results_reverified/` and
  `results_reverified2/` copies: zero differences, not even timestamps.
  These two transferred cleanly and hadn't drifted.
- **`results_first_run/`** did **not** match the local `results/` copy, and
  that's expected: `results/` is the patched, merged ground-truth tree
  described above, not a copy of this raw archive. The diff was consistent
  with exactly that patching (17 cells' `05-bench/` swapped, 3 cells'
  gap-filled iterations added, load-profile cleanup timestamps from
  2026-08-11 on the affected `meta.json` files) plus the `TimeCapsuleNotesVault`
  leftovers noted above. No unexplained data loss was found in this
  comparison; a handful of local-only, very large `backend.log` files from
  one crash-looping sample (`SplitNestSharedExpenseLedger`, Rust-Actix,
  claude-opus-4-8) accounted for most of the raw size gap and were
  diagnostic container logs, not benchmark data — the exact kind of thing
  the trimming above now strips automatically.

# Iterative Experiment Loop

Draft content for Methods §3 — the end-to-end workflow you described.

---

## Overview

Each **experiment** runs multiple **iterations** on a single BaxBench **sample**
(one scenario + framework + model configuration). The loop is:

```text
for iteration in 000 .. N:
    decision (skip on 000)
    → code
    → spec
    → deploy
    → bench
    → outcome / feedback
```

The agent retains **one persistent conversation thread** per
`(sample, experiment)`, stored in `k8s-experiments/<slug>/conversation.json`.
Each LLM phase **appends** a new user turn (and the model’s reply) to that
history, so prior prompts and responses from earlier iterations remain in
context.

To avoid repeating large payloads every turn, prompts use **slimming**:
benchmark telemetry is embedded inline in the **decision** phase; later **spec**
and **code** phases reference prior turns by pointer (e.g. “see `<SPEC>` in
conversation history”) rather than re-pasting full Locust output or code.
Structured artifacts (`iteration_feedback.json`, `experiment_summary.md`,
`prompt.log`) still exist for orchestration, plotting, and reproducibility—but
they complement the conversation, they do not replace it.

---

## Baseline (iteration 000)

**Purpose:** Establish a working application and a deployable deployment layout.

1. **Code** — LLM generates application code from the BaxBench scenario (OpenAPI).
   - Multiple attempts allowed (`baseline_code_max_attempts`).
   - Each attempt must pass **functional tests** before proceeding.
   - On success: Docker image built; code snapshot stored under the iteration.

2. **Spec** — LLM proposes `spec.yaml` (replicas, resources, DB settings) given cluster capacity.
   - Multiple attempts allowed (`baseline_spec_max_attempts`, default 5).
   - Each attempt: static validation → deploy probe (pods Ready, endpoints) → accept or re-prompt.

3. **Deploy** — Framework applies manifests to the cluster.

4. **Bench** — Locust adaptive load test; diagnostics collected.

5. **Outcome** — Metrics and narrative feedback written for iteration 001.

**Folder naming:** `iteration-000-baseline/`

---

## Refinement iterations (001 … N)

**Purpose:** Improve performance using benchmark feedback.

1. **Decision** — LLM chooses:
   - **deployment** (refine `spec.yaml`), or
   - **code** (refine application implementation).

   Can be forced by `--k8s-refinement deployment|code` or `auto` (default).

2. **Code**
   - *Deployment path:* reuse code from last successful iteration (no regen).
   - *Code path:* single LLM refinement attempt + functional tests (fail-fast).

3. **Spec**
   - *Deployment path:* single LLM spec attempt (fail-fast).
   - *Code path:* reuse last passing `spec.yaml`.

4. **Deploy → Bench → Outcome** — same as baseline.

**Folder naming:** `iteration-001-spec/`, `iteration-002-code/`, etc.

---

## What the agent sees between iterations

Aggregated into the next phase’s prompts (especially spec and decision):

| Signal | Source |
|--------|--------|
| Per-endpoint Locust stats | `locust/results/*_stats.csv` |
| Top errors | Locust failure logs |
| Cluster / pod utilization | `diagnostics/kubernetes/cluster/kubectl_top_*.csv` |
| Previous deployment spec | `spec/spec.yaml` |
| Narrative summary | `iteration_feedback.txt` / `.json` |
| Optional trajectory | `experiment_summary.md` (append-only) |

---

## Iteration budget

- CLI: `--k8s-iterations N` → baseline + N refinement phases.
- Failed baseline can **abort the entire sample** (no point refining broken code).
- Failed refinement iteration is recorded (`*-code-failed`, `*-spec-failed`) but does not advance the “latest good” lineage.

---

## Intuitive story (thesis prose)

> We first obtain a correct application and a cluster-feasible deployment through
> a baseline phase with retries. Each subsequent iteration presents the agent with
> load-test results and cluster diagnostics from the last successful deployment.
> The agent then decides whether to tune deployment parameters or application code,
> subject to validation and functional tests, and the system redeploys and
> re-benchmarks. This repeats for a fixed number of iterations.

---

## Cross-references

- Phase details: files `04`–`09`.
- Failure semantics: [10-failure-handling.md](10-failure-handling.md).
- On-disk layout: `docs/k8s_approach.md` (iteration directory tree).

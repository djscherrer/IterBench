# Phase: Code Generation & Functional Tests

Corresponds to `02-code/`.

---

## Role

Produce a **container-ready application** that implements the BaxBench scenario
and passes functional correctness tests.

---

## Modes

| Refinement action | Code stage behaviour |
|-------------------|----------------------|
| `baseline` | LLM codegen from scenario; **multiple attempts** |
| `code` | LLM code refinement using benchmark + FT feedback; **single attempt** |
| `deployment` | **Reuse** code tree from latest successful iteration (lineage copy) |

---

## Functional tests

- BaxBench scenario tests (`Task.test_code`) — same harness as non-K8s BaxBench.
- Code must pass **before** image build / deploy proceeds.
- On refinement code failure: iteration marked `*-code-failed`; live code reverts to last passing snapshot.

**Thesis point:** Performance optimization never trades off **correctness** at the API level; failed code does not reach the cluster.

---

## Image build

After tests pass:

1. Docker image built from generated sources.
2. Tagged and pushed to cluster registry (if enabled).
3. `image_id` recorded for deploy stage.

---

## Baseline retries

Document explicitly:

- `baseline_code_max_attempts` — configurable upper bound on LLM retries.
- Failure on baseline after all attempts → **sample abort** (experiment stops for this sample).

Refinement: one shot only (`max_attempts = 1`).

---

## Artifacts

- `02-code/code/` — source tree
- `02-code/functional_tests/` — iteration-scoped test artifacts (refinement)
- Initial sample-level `code/` at `sampleN/code/` — immutable after first successful baseline codegen

---

## What to write

- Inputs to codegen prompt: scenario OpenAPI, framework template, `high_performance` hint, prior FT failures (if any).
- For **code refinement**: benchmark feedback appended — agent instructed to improve hot paths, pooling, queries, etc., without breaking API contract.
- Clarify that **deployment refinement does not regenerate code** — isolates the effect of K8s tuning.

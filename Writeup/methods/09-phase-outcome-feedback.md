# Phase: Feedback Aggregation (end of bench)

After a successful Locust run under `05-bench/`, the bench stage closes the
iteration by writing feedback artifacts consumed by the next iteration.

---

## Role

Close the iteration by:

1. Labelling success/failure for lineage tracking.
2. Compressing bench + diagnostics into **agent-consumable feedback**.
3. Appending human-readable experiment trajectory.

---

## Primary feedback artifact

**`iteration_feedback.json`** (+ human-readable `.txt` sibling) in the bench directory.

Typical contents (describe categories, not every key):

| Category | Examples |
|----------|----------|
| Load test summary | Goodput, RPS, latency percentiles, error excerpts |
| Per-endpoint breakdown | Markdown table from Locust CSVs |
| Resource pressure | min/avg/max CPU & memory from `kubectl top` |
| Deployment context | Iteration id, spec diff pointer, image id |
| Failure metadata | If iteration failed earlier, `failure_kind` for routing |

Next iteration’s **decision**, **spec**, and **code** prompts include pointers to this file (and logs), not the full raw CSV dump inline.

---

## Lineage rules

| Outcome | Effect on next iteration |
|---------|--------------------------|
| `ok` | Feedback becomes `prior_feedback`; code/spec snapshots advance |
| `code-failed` / `spec-failed` | Folder suffix `-failed`; **excluded** from “latest good” lineage; forces retry path |
| `deploy-failed` | Recorded; decision LLM chooses next action |

---

## Experiment trajectory

**`experiment_summary.md`** (append-only per sample/experiment):

- After each spec: deployment diff vs. previous iteration + LLM rationale excerpt.
- After each bench: time range, adaptive ramp table, aggregate stats, top errors.

Useful for thesis **case studies** — walk through one scenario iteration by iteration.

---

## Conversation model (how to describe agent memory)

> Each experiment uses **one conversational LLM session** per sample
> (`conversation.json`). Every phase appends its prompt and the model reply, so
> the agent sees prior rounds in full message history. New turns are **slim**:
> decision embeds benchmark telemetry once; spec/code phases point at earlier
> `<SPEC>`, `<CODE>`, and decision turns instead of duplicating Locust dumps.
> Structured files (`iteration_feedback.json`, etc.) support the framework and
> analysis; they are not the sole memory channel.

Reference: `docs/k8s_conversational_prompt_slimming.md`, `src/k8s_bench/session.py`

---

## Iteration index file

**`iteration.log`** at iteration root — header + outcome line (cheap index).

**`meta.json`** — refinement action, folder kind, based-on iteration.

---

## What to write in Methods

- Feedback is **lossy compression** by design (tables + highlights, not full telemetry).
- Reproducibility: a third party can replay prompts from `*_prompt.log` files.
- Optional: mention plotting pipeline for goodput trajectories across iterations.

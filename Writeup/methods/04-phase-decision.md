# Phase: Decision (Refinement Routing)

Corresponds to on-disk folder `01-decision/` — **skipped for iteration 000**.

---

## Role

Before code and spec work in refinement iterations, the system determines **which
lever** the agent may pull:

| Action | Meaning in pipeline |
|--------|---------------------|
| `deployment` | Refine `spec.yaml` (replicas, resources, placement, DB topology) |
| `code` | Refine application source (performance-oriented code changes) |

Baseline hard-codes `refinement_action = baseline` (no LLM decision).

---

## When the decision LLM runs

| Situation | Behaviour |
|-----------|-----------|
| Iteration 000 | No decision stage |
| Prior iteration **succeeded** | LLM decision (`auto` mode) or forced by `--k8s-refinement` |
| Prior iteration failed in **code** or **spec** stage | **Forced** retry of same path (LLM skipped) — see [10-failure-handling.md](10-failure-handling.md) |
| Prior iteration failed in **deploy** (or bench setup) | LLM decides freely |

---

## Prompt contents (what to mention in Methods)

The decision prompt includes pointers to:

- Prior iteration feedback (`iteration_feedback.json` / `.txt`)
- Latest passing code and spec locations
- Cluster capacity summary
- Iteration index and remaining budget

The LLM responds with structured tags, e.g. `<DECISION>deployment</DECISION>` and `<RATIONALE>…</RATIONALE>`.

**Persisted artifacts:** `decision/decision.json`, logged in `01-decision/phase.log`.

---

## Thesis bullets

- Motivation: deployment tuning and code optimization are **coupled** but expensive to change together; routing keeps each iteration focused.
- The decision is **observable** (logged rationale) — useful for qualitative analysis in Results.
- Mention `--k8s-refinement auto|deployment|code` as ablation / control for your experiments.

---

## Suggested figure

Small flowchart:

```text
prior outcome OK? ──no──► failure kind?
       │                      │
      yes              code/spec ──► force same path
       │                      │
       ▼                 deploy ──► LLM decides
  LLM decision
  (or forced mode)
```

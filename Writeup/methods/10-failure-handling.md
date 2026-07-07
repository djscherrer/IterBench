# Failure Handling & Iteration Continuity

Draft for Methods §5 (or subsection under the iterative loop).

---

## Your intuition (validated against code)

> If code or spec refinement fails, the next iteration is **forced** to retry that
> same refinement type with feedback about what failed.  
> If deployment fails, the agent is **free to choose** code or deployment refinement.

This matches `forced_refinement_action_after_failure()` in `execute.py`:

| `failure_kind` | Next iteration forced action |
|----------------|------------------------------|
| `code` | `code` |
| `spec` | `deployment` (spec path) |
| `deploy` | *none* → decision LLM chooses |
| other / unknown | decision LLM chooses |

---

## Per-stage failure outcomes

| Stage fails | Folder suffix | Lineage | Sample abort? |
|-------------|---------------|---------|---------------|
| Code (baseline) | `code-failed` | unchanged | **yes** (after max attempts) |
| Code (refinement) | `*-code-failed` | reverts to last good code | no |
| Spec (baseline) | retry in same iteration | — | yes if all attempts exhausted |
| Spec (refinement) | `*-spec-failed` | spec unchanged | no |
| Deploy | `deploy-failed` | code/spec unchanged | no |
| Bench | logged in outcome | prior deploy may still stand | depends on config |

Failed refinement folders are **excluded from the feedback chain** — the agent’s next prompt still references the last **successful** iteration.

---

## Baseline failure is special

If iteration 000 fails irrecoverably:

- No meaningful refinement baseline exists.
- Sample aborts (`abort_sample` flag) — document how many samples this affected in Results.

---

## Namespace cleanup

- Before each deploy and after each bench: delete all `baxbench-*` namespaces.
- Prevents resource leakage across iterations on a **fixed-size lab cluster**.
- Results on disk are retained even when cluster state is torn down.

Env override: `BAXBENCH_K8S_CLEANUP=false` (mention if you disabled for debugging).

---

## Thesis narrative

Use a short subsection with a decision table (see above) — examiners like explicit
**control logic** separate from LLM behaviour.

Optional: distinguish **validation failures** (spec never deployed) from **runtime failures** (OOM during bench) — different signals in feedback.

---

## Suggested LaTeX table caption

*"Iteration routing after failure. Code and spec failures trigger a mandatory retry of the same refinement lever; deploy failures leave the choice to the refinement agent."*

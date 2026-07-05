# K8s Conversational Prompt Slimming

Design for slim prompts in the iterative k8s experiment loop. One `conversation.json` thread per `(sample, experiment)`; each LLM call receives full history plus a new user turn.

## Information architecture

| Phase | Telemetry (Locust, diagnostics) | Artifacts (code, spec) |
|-------|--------------------------------|------------------------|
| **Decision** | Embedded inline (sole telemetry sink) | Pointers to history |
| **Spec** | Pointer to decision turn above | Pointers to history |
| **Code** | Pointer to decision turn above | Pointer to last passing code; FT failure blocks inline |

Implementation: [`src/k8s_bench/prompt_helpers.py`](../src/k8s_bench/prompt_helpers.py)

## Decision phase

**Builder:** `build_refinement_decision_prompt` in `refinement/decision.py`

- Role: performance optimization strategist
- Progress: neutral budget wording
- Paths: `deployment` (all K8s levers) vs `code` (must pass FT; spec unchanged this iteration)
- Guardrails: failed-attempt anti-examples; do not pick `code` on `[INFRASTRUCTURE FAILURE]`
- Full `IterationFeedback.to_prompt_text(include_spec_yaml=False)`
- Dynamic pointers via `resolve_artifact_pointers()`

## Spec phase

**Builder:** `build_k8s_spec_prompt` in `spec/generation.py`

- Goal + optimization objective + strategic progress ("bold early, consolidate late")
- Context + artifact pointers + decision telemetry pointer
- Cluster capacity, scheduling rules, spec field reference, output template
- No inline load test block, no `previous_spec.yaml`, no app code excerpt

## Code refinement phase

**Builder:** `build_code_refinement_prompt` in `refinement/code.py`

- Full scenario `build_prompt` (API contract)
- Refinement task + progress + optimization objective
- Context + artifact pointers + decision telemetry pointer
- K8s deployment context (read-only)
- Prior / same-iteration functional test failure blocks (must-fix)
- No inline benchmark feedback, no inline full codebase

## Artifact pointers

Resolved from disk:

- `prior_iteration_code_dir()` → copy source for deployment/spec lineage (N−1)
- `find_latest_code_snapshot_iteration()` → codegen iteration folder name for prompt pointers
- `latest_spec_path()` → `<SPEC>` iteration folder name

## System prompt

Global LLM system prompt remains `"You are an experienced full-stack developer"` (`llm/config.py`). Phase-specific roles are only in user prompts.

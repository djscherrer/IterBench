# Failure Handling (per phase)

Aligned with `src/k8s_bench/failure/record.py` and `forced_refinement_action_after_failure`.

## Cross-iteration rule

| Prior failed phase | Next refinement |
|--------------------|-----------------|
| `code` | **Forced** `code` (decision LLM skipped) |
| `spec` | **Forced** `deployment` |
| `deploy` / `bench` / `decision` | Decision LLM **chooses**; gets failure `to_prompt_block()` |
| Baseline `000` any terminal fail | **Abort sample** |

Failed iterations excluded from “latest good” lineage; success uses bench feedback instead of failure (never both).

## Kinds → what the next LLM sees

### Decision
Kinds: `llm_call`, `llm_parse`  
Feedback: kind + error/summary. Sample continues.

### Code
Kinds: `functional_test`, `docker_build`, `infrastructure`, `llm_call`, `llm_parse`, `ft_runner`  
Feedback: FT → failed/passing tests + evidence; build → compile log; infra → explicit “not an app bug”.  
Next: forced **code**.

### Spec
Kinds: `spec_validation`, `llm_call`, `llm_parse`  
Feedback: validation errors/warnings listed.  
Next: forced **deployment**.

### Deploy
Kinds: `image_pull`, `unschedulable`, `crashloop`, `oomkilled`, `readiness_probe`, `endpoints_unavailable`, `kubectl_apply`, `namespace_cleanup`, `timeout`, `unknown`  
Feedback: kind + wait details + diagnostics excerpt; guide to fix replicas/resources/placement.  
Next: free decision.

### Bench
Kinds (harness fail): `locust_infra`, `target_unreachable`, `timeout_or_stall`, `unknown`  
Note: a finished load test with poor goodput is still **ok** for routing → normal decision with goodput feedback.  
Next (harness fail): free decision + bench log excerpt.

## Thesis placement

Prefer **Failures** paragraphs under each Methods phase (§3.4); keep one **routing summary table** (§3.5).

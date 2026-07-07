# K8s iterative experiment: stage failure taxonomy

This document summarizes **which failures can surface in each stage**, and **what evidence BaxBench persists** for debugging and for LLM retry prompts.

## Common structure

Terminal failures are persisted as an `IterationFailure` envelope, which contains:

- **`phase`**: one of `decision | code | spec | deploy | bench`
- **`terminal`**: a phase-specific failure record (below)
- **attempt history** (for phases that retry)

Each phase record includes at least:

- **`summary`**: short human-readable description
- **`iteration_id`** (and often `attempt`)
- optional **evidence** (trimmed logs, kubectl wait details, raw model output)

## 01-decision (routing: deployment vs code)

### Failure records

- **`DecisionFailureRecord(kind="llm_call")`**
  - **When**: model call fails after retries (budget, transport, provider error, exception)
  - **Evidence**: `llm_error`
- **`DecisionFailureRecord(kind="llm_parse")`**
  - **When**: model returns output that does not match `<DECISION>deployment|code</DECISION>`
  - **Evidence**: `diagnostic_excerpt` contains raw model output (truncated)

### Notes

- Decision failures are terminal for the iteration; the run aborts the sample.

## 02-code (codegen + functional tests)

### Failure records (`CodeFailureRecord`)

- **`kind="llm_call"`**
  - **Evidence**: `llm_error`
- **`kind="llm_parse"`**
  - **Evidence**: `llm_error` (what was missing / malformed)
- **`kind="docker_build"`**
  - **Evidence**: `diagnostic_excerpt` (compile/build log excerpt)
- **`kind="functional_test"`**
  - **Evidence**:
    - `num_passed_ft` / `num_total_ft`
    - `failed_tests[]` with per-test log tail + container error excerpt
    - `passed_tests[]` list (regression guard)
- **`kind="ft_runner"`**
  - **Evidence**: runner error in `llm_error` / `summary`
- **`kind="infrastructure"`**
  - **When**: harness failures (e.g. port binding issues, container could not start, DB harness broken)
  - **Evidence**: `infrastructure_failure{kind,description,evidence}` + blocked tests list

## 03-spec (generate + validate `spec.yaml`)

### Failure records (`SpecFailureRecord`)

- **`kind="llm_call"`**
  - **Evidence**: `llm_error`
- **`kind="llm_parse"`**
  - **Evidence**: `llm_error`
- **`kind="spec_validation"`**
  - **When**: static validation fails
  - **Evidence**: `errors[]` + `warnings[]`

## 04-deploy (render manifests + `kubectl apply` + readiness probe)

### Failure records (`DeployFailureRecord`)

**`kind`** values (root-cause bucket):

- `image_pull` | `namespace_cleanup` | `unschedulable` | `crashloop` | `oomkilled`
- `readiness_probe` | `endpoints_unavailable` | `kubectl_apply` | `timeout` | `unknown`

Typical sources:

- **Manifests apply but resources never become Ready** (timeouts, crashloops, unschedulable pods)
- **Service endpoints never become ready**
- **Namespace cleanup / apply errors** (kubectl failures)

Captured evidence:

- **`reason`** and **`summary`**
- **`details`** including per-resource **kubectl wait** details (`wait/<resource>`)
- **`diagnostic_excerpt`**: pod snapshot (`kubectl get/describe/logs`, trimmed)

## 05-bench (Locust + diagnostics)

### Failure records (`BenchFailureRecord`)

**`kind`** values:

- `locust_infra` | `target_unreachable` | `timeout_or_stall` | `unknown`

- **Evidence**: `summary` + `diagnostic_excerpt` (tail of `05-bench/bench.log`)

## Suggested future refinements (not yet implemented)

### Bench diagnostics enrichment

(Optional) Also capture Locust worker stderr from distributed runs when available.


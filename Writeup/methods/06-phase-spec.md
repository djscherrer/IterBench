# Phase: Deployment Specification

Corresponds to `03-spec/`.

---

## Role

The agent outputs a **constrained deployment specification** (`spec.yaml`), not
raw Kubernetes YAML. The framework validates it and renders manifests.

This is the core of your thesis contribution: **LLM-guided deployment optimization**
under fixed cluster resources.

---

## Spec schema (agent-controlled knobs)

Summarize as a table in the thesis (see also `docs/k8s_approach.md`).

### Backend (stateless, horizontally scalable)

| Field | Effect |
|-------|--------|
| `replicas` | Pod count behind Service |
| `resources.cpu_request`, `memory_request`, limits | Per-pod scheduling / throttling |
| `placement.workers` | Optional node allow-list |
| `placement.spread_replicas` | Spread pods across nodes (anti-affinity) |

### Database (Postgres)

| Field | Effect |
|-------|--------|
| `replicas` | 1 = single pod; N>1 = primary + read replicas |
| `max_connections` | Must cover `backend.replicas × pool_size` |
| `resources.*` | Per DB pod; must fit on one worker |
| `placement.worker` / `placement.workers` | Pin or restrict DB placement |

---

## Validation (framework — not LLM)

**Static validation** before deploy:

1. Each pod’s **requests** must fit on **at least one worker** individually.
2. Cluster-wide request totals must not exceed advertised capacity.
3. Connection pool inequality: `replicas × pool_max ≤ database.max_connections`.

**Deploy probe** (dynamic):

- Apply manifests, wait for pods Ready, check Service endpoints.
- No HTTP load yet — distinguishes “schedulable layout” from “performant under load”.

---

## Modes

| Refinement action | Spec stage behaviour |
|-------------------|----------------------|
| `baseline` | LLM spec; **multiple attempts** (validation + probe loop) |
| `deployment` | LLM spec refinement with benchmark feedback; **single attempt** |
| `code` | **Reuse** `spec.yaml` from latest successful iteration |

---

## Baseline vs. refinement (important for Methods)

> **Baseline:** the agent may retry until the cluster accepts a layout or attempts are exhausted.  
> **Refinement:** one spec generation per iteration; failure → `*-spec-failed`, excluded from lineage.

---

## Prompt design philosophy

- Prompt describes **field semantics and hard constraints** only.
- **No recommended numeric ranges** — agent must learn tuning from benchmark feedback (supports your research narrative).

---

## Artifacts

- `03-spec/spec.yaml` — accepted specification
- `03-spec/spec_gen_prompt.log` — full LLM prompt
- `manifests/` — rendered Kubernetes objects (framework output)

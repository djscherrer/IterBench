# Design Principles & Constraints

Standalone section or woven into Architecture + Spec phases.

---

## 1. Bounded agent search space

**Problem:** Raw Kubernetes YAML is huge, easy to invalidate, hard to compare across runs.

**Solution:** Agent edits `spec.yaml` only; framework renders Deployments, Services, affinity rules, DB topology.

**Thesis angle:** Enables fair comparison — differences are deployment *decisions*, not syntactic accidents.

---

## 2. Framework owns infrastructure

Fixed by BaxBench / your extension:

- Network wiring between app and Postgres
- Namespace lifecycle
- Manifest rendering and `kubectl` apply
- Locust orchestration and diagnostics collectors
- Validation rules and probe logic

Agent never directly runs shell on cluster nodes.

---

## 3. Correctness before performance

Functional tests gate all code paths that produce new images.

Deployment probe gates all specs before load.

Performance metrics only attach to **correct, ready** deployments.

---

## 4. Learn from feedback, not from hand-tuned ranges

Spec prompts document semantics and hard constraints but omit recommended numeric values — the agent must interpret Locust and `kubectl top` signals.

Supports claims about **autonomous** deployment optimization.

---

## 5. Fail-fast refinement, generous baseline

| Phase | Retry policy | Rationale |
|-------|--------------|-----------|
| Baseline | Many attempts | Must bootstrap a working system |
| Refinement | Single attempt per lever | Fixed iteration budget; clear attribution per iteration |

---

## 6. Reproducibility & observability

Every LLM call logged (`prompt.log`, `phase.log`).

Iteration directories are self-contained experiment records.

Cluster profile codified in `profiles.py` — not ad-hoc host strings in prompts.

---

## 7. Separation of concerns (summary table)

| Concern | Owner |
|---------|-------|
| API correctness | BaxBench scenarios + functional tests |
| Container packaging | Framework Dockerfile templates |
| Scheduling / resources | Agent `spec.yaml` |
| Validity of spec | Framework validator |
| Performance measurement | Locust + diagnostics |
| Refinement strategy | Agent (decision + spec/code prompts) |

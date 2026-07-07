# Methods Chapter — Proposed Outline

Use this as the skeleton for your LaTeX `\chapter{Methods}` (or equivalent).
Adjust numbering to match your faculty template.

---

## 1. Introduction to the evaluation setup

**Goal:** Orient the reader before diving into machinery.

- Research question restated in operational terms (what is being optimized, under what constraints).
- High-level claim: BaxBench generates applications; your extension adds an **iterative deploy–benchmark–refine loop** on a real Kubernetes cluster.
- Brief pointer to later sections (architecture → loop → phases → metrics).

*Suggested length:* ½–1 page.

---

## 2. System architecture

→ See [02-system-architecture.md](02-system-architecture.md)

- Cluster topology: control-plane node, worker nodes, optional private registry.
- Load-generation topology: Locust master + workers (may overlap with cluster hosts).
- BaxBench orchestrator host: runs LLM calls, builds images, drives `kubectl`, SSH to Locust.
- Responsibility split: **framework** (infra, YAML rendering, probes, benchmarks) vs. **agent** (code + deployment parameters).

*Include:* one architecture figure (see [12-figures-and-tables.md](12-figures-and-tables.md)).

---

## 3. Experimental workflow (iterative loop)

→ See [03-iterative-experiment-loop.md](03-iterative-experiment-loop.md)

- One **sample** = one (scenario × framework × model × temperature × …) run directory.
- **Experiment slug** groups iterations under `k8s-experiments/<slug>/`.
- **Iteration 000 (baseline):** generate initial code + deployment spec, deploy, benchmark.
- **Iterations 001…N (refinement):** agent receives feedback, decides code vs. deployment path, repeats pipeline.
- Termination: fixed `--k8s-iterations N` or sample abort on baseline failure.

*Include:* pipeline / sequence diagram of one full iteration.

---

## 4. Iteration phases (detailed)

Present as subsections **in pipeline order** (matches how the system runs).

| § | Phase | Detail file |
|---|-------|-------------|
| 4.1 | Decision (refinement routing) | [04-phase-decision.md](04-phase-decision.md) |
| 4.2 | Code generation & functional tests | [05-phase-code.md](05-phase-code.md) |
| 4.3 | Deployment specification | [06-phase-spec.md](06-phase-spec.md) |
| 4.4 | Deploy & readiness probe | [07-phase-deploy.md](07-phase-deploy.md) |
| 4.5 | Load benchmark | [08-phase-benchmark.md](08-phase-benchmark.md) |
| 4.6 | Outcome & feedback aggregation | [09-phase-outcome-feedback.md](09-phase-outcome-feedback.md) |

**Alternative ordering for the thesis:** some authors prefer *baseline first* as §4, then *refinement loop* as §5 with the same sub-phases. Either works if you clearly separate baseline retries from refinement fail-fast behaviour.

---

## 5. Failure handling and iteration continuity

→ See [10-failure-handling.md](10-failure-handling.md)

- Forced retry after code/spec failure.
- Free choice after deploy failure.
- Failed iterations excluded from feedback lineage.
- Namespace cleanup between runs.

---

## 6. Design principles and constraints

→ See [11-design-principles.md](11-design-principles.md)

- Constrained deployment search space (`spec.yaml` schema).
- Cluster-capacity validation (per-worker fit, connection pool budget).
- Reproducibility: deterministic framework rendering, logged prompts, artifact layout.

---

## 7. Metrics and evaluation criteria *(optional in Methods, required somewhere)*

If your faculty puts metrics in **Evaluation** instead, cross-reference here.

- **Goodput** / sustainable throughput under adaptive load profile.
- Latency percentiles (e.g. p95), error rate.
- Resource utilization (CPU/memory from `kubectl top`, host metrics).
- Iteration-level success/failure labels.
- Comparison baselines (single-shot deploy, deployment-only refinement, etc.).

---

## 8. Implementation summary *(short)*

- BaxBench extension: `--mode k8s-bench`, `scripts/bench_k8s.sh`.
- Key packages: `k8s_bench/`, `locust_bench/`, `bench_diagnostics/`.
- Cluster profiles: `k8s_bench/cluster/profiles.py` (e.g. Emulab topology).

*Keep brief — full implementation belongs in appendix or repository README.*

---

## Writing tips for LaTeX

- Use `\texttt{iteration-000-baseline}` for on-disk folder names.
- Use *control-plane node* / *worker node* for Kubernetes; *load master* / *load worker* for Locust.
- Refer to the agent as **LLM agent** or **refinement agent** consistently.
- Methods describes **what** and **how**; Results chapter reports **numbers**.

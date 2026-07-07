# Related Work — Paper Analysis

Working notes for the Related Work chapter. Covers the PDFs in your
`related_work` collection (local OneDrive + `Writeup/related_work/papers/`),
plus BaxBench, HumanEval, and SWE-bench as anchor citations.

---

## PDF inventory (12 papers in your collection)

| File | Paper | Year | Venue / ID |
|------|-------|------|------------|
| `Borg, Omega, and Kubernetes.pdf` | Borg, Omega, and Kubernetes | 2016 | ACM Queue |
| `Clover.pdf` | Clover: Closed-Loop Verifiable Code Generation | 2023 | arXiv:2310.17807 |
| `CodeFlowBench.pdf` | CodeFlowBench: Multi-turn Iterative Benchmark for Complex Code Generation | 2025 | arXiv:2504.21751 |
| `CWEval.pdf` | CWEval: Outcome-driven Evaluation on Functionality and Security | 2025 | arXiv:2501.08200 |
| `Glia.pdf` | Glia: A Human-Inspired AI for Automated Systems Design and Optimization | 2025 | arXiv:2510.27176 |
| `Just-in-Time Systems.pdf` | The Time is Here for Just-in-Time Systems (Jitskit) | 2025 | arXiv:2605.24096 |
| `Resource Management with DRL.pdf` | Decima: Learning Scheduling Algorithms for Data Processing Clusters | 2019 | NSDI |
| `SecRepoBench.pdf` | SecRepoBench: Secure Code Completion in Real-World Repositories | 2026 | arXiv:2504.21205 |
| `SWE-Perf.pdf` | SWE-Perf: Code Performance Optimization on Real-World Repositories | 2026 | ICML 2026 |
| `Synchromesh.pdf` | Synchromesh: Reliable Code Generation from Pre-trained Language Models | 2022 | arXiv:2201.11227 |
| `Verge.pdf` | VERGE: Formal Refinement and Guidance Engine for Verifiable LLM Reasoning | 2026 | arXiv:2601.20055 |
| `VibeServe.pdf` | VibeServe: Can AI Agents Build Bespoke LLM Serving Systems? | 2026 | arXiv:2605.06068 |

**Also cite (no PDF required in folder):**

| Paper | Role |
|-------|------|
| HumanEval (Chen et al., 2021) | Function-level codegen baseline |
| SWE-bench (Jimenez et al., 2024) | Repo-level SE baseline |
| BaxBench (Vero et al., 2025) | **Anchor** — your thesis extends this |

---

## Recommended Related Work structure

```
Chapter 2 — Related Work

§2.1  LLM code generation and backend evaluation benchmarks
      HumanEval, SWE-bench → BaxBench, CWEval, SecRepoBench, CodeFlowBench

§2.2  Reliable and verifiable LLM code generation
      Synchromesh, Clover [, VERGE]

§2.3  Deployment configuration and cluster tuning          ← fills the K8s gap
      Borg/Kubernetes, HPA/VPA, Decima, Glia

§2.4  LLM-driven performance optimization
      SWE-Perf, VibeServe, Jitskit

§2.5  Positioning of this work
      Gap paragraph + comparison table
```

### Page budget (master's thesis)

| Section | Depth | ~Length |
|---------|-------|---------|
| §2.1 | Medium — ladder + 4 backend papers | 1–1.5 pp |
| §2.2 | Light–medium | 0.5–1 pp |
| §2.3 | Medium — **new, important** | 0.75–1 pp |
| §2.4 | Deep — core contribution context | 1.5–2 pp |
| §2.5 | Short | 0.5 pp |

---

## §2.1 — LLM code generation and backend evaluation benchmarks

### HumanEval (background — cite, do not analyse deeply)

**Gist:** Single-function Python synthesis from docstrings; pass@k metric.

**Statement:** Establishes the earliest standard for LLM code correctness at function granularity.

---

### SWE-bench (background)

**Gist:** Real GitHub issues; repo-level patch generation and test verification.

**Statement:** Moves evaluation from isolated functions to repository context—still focused on correctness, not deployment performance.

---

### BaxBench (ESSENTIAL)

**Gist:** 392 tasks, 28 scenarios, full backend apps; functional + security tests in Docker.

**Connection:** Your direct predecessor. Same scenarios/tests; you add K8s, iteration, goodput.

**Statements:**
- "BaxBench evaluates complete backends for correctness and security but not orchestrated performance."
- "We retain BaxBench's application scenarios and functional test suite."

---

### CWEval (`CWEval.pdf`) — HIGH

**Gist:** Joint functionality + security evaluation with rigorous task specs and CodeQL oracles; prior security benchmarks overestimate safety.

**Connection:** Peer to BaxBench on correctness+security; neither measures deploy performance.

**Sample sentence:**
> CWEval jointly evaluates functionality and security with outcome-driven oracles; like BaxBench, it does not deploy services under load on a cluster.

---

### SecRepoBench (`SecRepoBench.pdf`) — MEDIUM–HIGH

**Gist:** 318 secure code-completion tasks in 27 C/C++ repos; agents evaluated with unit tests + OSS-Fuzz; agents beat standalone LLMs.

**Connection:** Repo-level **agent** evaluation for security; you evaluate agents for **deployment performance** on BaxBench backends.

**Sample sentence:**
> SecRepoBench benchmarks code agents on secure completion in real repositories; our agent loop instead targets deployment configuration and measured goodput.

---

### CodeFlowBench (`CodeFlowBench.pdf`) — HIGH

**Gist:** Multi-turn "codeflow"—reuse existing functions across turns; 5k+ comp problems + GitHub repos; performance drops vs single-turn as dependency complexity grows.

**Connection:** Best **iterative benchmark** analog; you iterate on code **and** `spec.yaml` with **runtime** feedback.

**Sample sentence:**
> CodeFlowBench formalises multi-turn iterative code generation; our loop is iterative at the deployment layer and closes each round with adaptive load testing rather than modular reuse alone.

---

### §2.1 gap sentence (use at end of section)

> Together, these benchmarks characterise whether LLM-generated software is correct, secure, or iteratively constructible—but not whether it **performs** under realistic **Kubernetes** deployment with tunable replicas, resources, and topology.

---

## §2.2 — Reliable and verifiable LLM code generation

### Synchromesh (`Synchromesh.pdf`) — MEDIUM

**Gist:** Constrained Semantic Decoding + Target Similarity Tuning; guarantees syntactic/semantic validity without fine-tuning the LM.

**Connection:** Same philosophy as constrained `spec.yaml`—narrow output space, validate before proceed.

**Sample sentence:**
> Synchromesh constrains decoding to valid programs; we constrain deployment edits to a schema validated by the framework before any cluster apply.

---

### Clover (`Clover.pdf`) — MEDIUM–HIGH

**Gist:** Closed-Loop Verifiable Code Generation—code + formal annotations + docstrings; consistency checking via verification; zero false positives on adversarial incorrect cases.

**Warning:** Not the 2025 CLOVER test-case-generation benchmark.

**Connection:** Closed loop generate→verify; you use functional tests + deploy gates + load benchmarks.

**Sample sentence:**
> Clover closes the loop with formal verification; we close it with executable BaxBench tests and empirical goodput measurement.

---

### VERGE (`Verge.pdf`) — OPTIONAL (2–3 sentences)

**Gist:** Neurosymbolic iterative refinement—claims → FOL → Z3; MCS-based feedback; ~18.7% uplift vs single-pass.

**Connection:** Structured external feedback for refinement (like your bench diagnostics), but for logical reasoning not systems.

**When to skip:** If page-limited, drop VERGE; Synchromesh + Clover suffice.

---

## §2.3 — Deployment configuration and cluster tuning

**Why this section exists:** Answers *"Why not just use Kubernetes autoscaling?"* Positions your work between **production practice**, **learned cluster management**, and **LLM system design**.

---

### Borg, Omega, and Kubernetes (`Borg, Omega, and Kubernetes.pdf`) — HIGH for §2.3

**Gist:** Decade of cluster management at Google; Borg → Omega → Kubernetes; scheduling, isolation, resource allocation at scale.

**Connection:** Establishes that production backends run on **orchestrated clusters** with declarative desired state—not isolated Docker containers like BaxBench.

**Statements:**
- "Kubernetes exposes deployment as declarative configuration (replicas, resources, placement) managed by a control plane."
- "Our work assumes this production model and asks whether LLMs can tune that configuration effectively."

**Sample sentence:**
> Burns et al. describe how cluster orchestrators allocate resources and enforce desired application state—the deployment layer our agent optimises beyond BaxBench's single-container evaluation.

---

### Kubernetes HPA / VPA (docs — cite in bib, no PDF needed)

**Gist:** HPA scales replica count from CPU/memory/custom metrics; VPA adjusts resource requests/limits; reactive, rule/metric-driven.

**Connection:** **Baseline automation** your agent should be compared against conceptually—not a reasoning agent, no joint code+deploy loop, no goodput search.

**Statements:**
- "HPA and VPA react to live metrics but do not reason about database topology, placement, or multi-iteration performance trade-offs."
- "They optimise within a fixed manifest; our agent may revise the manifest itself."

---

### Decima (`Resource Management with DRL.pdf`) — MEDIUM–HIGH for §2.3

**Gist:** Deep RL learns scheduling policies for Spark/data-processing clusters (NSDI 2019); outperforms hand-tuned heuristics; optimises **policies** within a fixed system architecture.

**Connection:** Shows **learned** cluster/resource management predates LLM agents—but targets schedulers/job placement, not BaxBench-style HTTP backends + `spec.yaml`.

**Statements:**
- "Decima learns scheduling policies with reinforcement learning; we use an LLM agent with natural-language reasoning and load-test feedback."
- "Like knob-tuning and policy-search methods, Decima holds application architecture fixed; we allow both code and deployment revision."

**Sample sentence:**
> Mao et al. demonstrate that learned policies can outperform hand-tuned cluster schedulers, but they do not generate or deploy full application stacks nor evaluate LLM-driven deployment specifications.

---

### Glia (`Glia.pdf`) — HIGH for §2.3

**Gist:** Multi-agent LLM architecture for **systems design**—reasoning, experimentation, analysis; applied to GPU cluster for LLM inference: new routing, scheduling, and **autoscaling** algorithms at human-expert level in less time.

**Connection:** **Closest neighbor** to "LLM tunes cluster behavior for performance." Difference: Glia designs **algorithms/policies** for a serving stack; you refine **Kubernetes deployment specs** for BaxBench apps with explicit goodput benchmarking.

**Statements:**
- "Glia combines LLM reasoning with empirical evaluation for systems mechanisms; our framework applies the same pattern to deployment parameters on Kubernetes."
- "Glia targets LLM inference clusters; we target general backend applications from BaxBench."
- "Glia generates interpretable routing/scheduling/autoscaling logic; we expose a constrained YAML deployment schema."

**Sample sentence:**
> Glia uses multi-agent LLMs with experimental feedback to design routing, scheduling, and autoscaling for inference clusters; we apply analogous iterative measurement to Kubernetes deployment tuning for full backend services.

---

### §2.3 gap sentence

> Existing cluster management—declarative orchestration, reactive autoscaling, learned schedulers, and emerging LLM-based mechanism design—does not evaluate whether an agent can **iteratively** refine **replica counts, resource limits, placement, and database topology** for **LLM-generated BaxBench applications** using **adaptive load tests** and **goodput** as the objective.

---

## §2.4 — LLM-driven performance optimization

### SWE-Perf (`SWE-Perf.pdf`) — HIGH (section opener)

**Gist:** 140 repo-level performance-optimization instances from GitHub PRs; large LLM–expert gap; Agentless, OpenHands evaluated.

**Connection:** Performance at **code** layer, largely one-shot; you: **deployment** layer, iterative runtime feedback.

*(See existing analysis in prior draft—content unchanged.)*

---

### VibeServe (`VibeServe.pdf`) — VERY HIGH

**Gist:** Outer/inner multi-agent loop; synthesises bespoke LLM serving runtimes; vLLM parity standard case; up to ~6× on non-standard scenarios.

**Connection:** Closest **structural** analog; differs in optimising **serving implementation code** vs **K8s deployment config**.

---

### Jitskit (`Just-in-Time Systems.pdf`) — VERY HIGH

**Gist:** Synthesise KV stores from spec cards; Planner/Coder/Evaluator/Critic + Auditor against reward hacking; 18/18 specs beat FASTER/RocksDB/Redis (up to 4.6×).

**Connection:** Same iterative loop + constrained spec; differs in **implementation synthesis** on one node vs **deployment** on multi-node K8s.

---

## §2.5 — Positioning

### Master gap paragraph (combine all sections)

> Prior work evaluates LLM-generated code for correctness and security (HumanEval, SWE-bench, BaxBench, CWEval, SecRepoBench) or iterative code construction (CodeFlowBench), typically in isolated environments. Reliability research constrains or verifies outputs before acceptance (Synchromesh, Clover). Cluster orchestration, autoscaling, learned scheduling (Kubernetes, Decima), and LLM-based mechanism design (Glia) automate infrastructure tuning but do not benchmark full LLM-generated backends under adaptive load. Recent agentic systems optimise **implementation-level** performance (SWE-Perf, VibeServe, Jitskit). **Our thesis fills the gap between BaxBench-style application evaluation and production deployment: an iterative agent loop that refines both application code and Kubernetes deployment configuration, scored by sustainable goodput on a real multi-node cluster.**

### Comparison table dimensions

Include rows for: optimisation target, feedback loop, runtime measurement, correctness gate, metric, environment, what the agent controls.

Compare at minimum: **SWE-Perf, Glia, VibeServe, Jitskit, This work**.

---

## Citation priority summary

| Priority | Papers |
|----------|--------|
| **Must cite deeply** | BaxBench, VibeServe, Jitskit, Glia |
| **Must cite** | HumanEval, SWE-bench, SWE-Perf, CWEval, CodeFlowBench, Borg/K8s, Decima |
| **Should cite** | Synchromesh, Clover, SecRepoBench, HPA/VPA |
| **Optional** | VERGE |

---

## BibTeX keys (`references.bib`)

| Paper | Key |
|-------|-----|
| HumanEval | `chen2021humaneval` |
| SWE-bench | `jimenez2024swebench` |
| BaxBench | `vero2025baxbench` |
| CWEval | `peng2025cweval` |
| SecRepoBench | `shen2026secrepobench` |
| CodeFlowBench | `wang2025codeflowbench` |
| Synchromesh | `poesia2022synchromesh` |
| Clover | `sun2023clover` |
| VERGE | `singh2026verge` |
| Borg/Kubernetes | `burns2016borg` |
| HPA | `kubernetes-hpa` |
| Decima | `mao2019decima` |
| Glia | `hamadanian2025glia` |
| SWE-Perf | `he2025sweperf` |
| VibeServe | `kamahori2025vibeserve` |
| Jitskit | `liu2025jitsystems` |

---

## LaTeX draft status

- `01-related-work-draft.tex` — full chapter draft with §2.1–§2.5 (updated to match this outline).

---

## What you do **not** need to add

- More security-only benchmarks (redundant with CWEval + SecRepoBench)
- Unrelated ML datasets (YCSB, etc.) unless you adopt them
- GitOps/Helm surveys unless your implementation uses them
- The 2025 CLOVER test-case paper (name collision with Clover 2023)

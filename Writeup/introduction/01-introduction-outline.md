# Introduction — Outline & Suggested Content

Based on the structure of the BaxBench paper (Vero et al., 2025) and
AutoBaxBuilder (von Arx et al., 2025), plus standard thesis introduction patterns.

---

## What an introduction does (general)

A thesis introduction follows a **funnel** structure:

1. **Broad context** — Why does this topic area matter? (1–2 paragraphs)
2. **Narrow to the problem** — What specific gap exists? (1–2 paragraphs)
3. **This work** — What does this thesis do about it? (1 paragraph)
4. **Contributions** — Bullet list of concrete contributions (½ page)
5. **Outline** — Road map of the remaining chapters (½ page)

Both reference papers follow this exact pattern. Below is how it maps to
**your** thesis.

---

## How BaxBench structures its introduction

| Element | What BaxBench writes |
|---------|----------------------|
| Broad context | LLMs can generate code at function level; for full automation they need to produce production-quality modules |
| Why backends | Practically relevant, hard to get right, security-critical |
| Gap | No benchmark evaluates both correctness **and** security for full applications |
| This work | BaxBench: 392 tasks, 28 scenarios, 14 frameworks, 6 languages |
| Key findings | Best model 62% correctness; ~50% of correct code is insecure |
| Significance | Progress here = progress toward autonomous secure software development |

---

## How AutoBaxBuilder structures its introduction

| Element | What AutoBaxBuilder writes |
|---------|----------------------------|
| Broad context | LLM code generation is rising → security concerns for deployed code |
| Shortcomings | Current evaluations test correctness/security separately or only at function level |
| Prior work + gap | BaxBench is rigorous but requires **significant manual expert effort**; benchmarks contaminate training data; must be updated and made harder |
| This work | Agentic LLM pipeline that generates scenarios + functional tests + exploits automatically |
| Validation | Compare against human experts on BaxBench scenarios |
| Results preview | Doubles BaxBench (40 new scenarios); 3 difficulty tiers; under $10/task |
| Contributions | Numbered list of 3 concrete contributions |

---

## Your thesis introduction — proposed structure

### §1.1 LLMs are increasingly generating production code

**Broad context.** Start with the trend:

- LLMs have progressed from function-level completion to generating full backend applications (cite: HumanEval/Chen et al. 2021, SWE-bench/Jimenez et al. 2024, BaxBench/Vero et al. 2025).
- Organizations are beginning to deploy LLM-generated code in production environments.
- Recent benchmarks (BaxBench) show that LLMs can generate functionally correct backend applications, though significant gaps remain in security.

**One paragraph, ~5–8 sentences.**

---

### §1.2 Correctness is not enough — performance under real deployment conditions matters

**Narrow the problem.** This is where you diverge from the papers:

- Existing benchmarks evaluate **correctness** (functional tests) and **security** (exploit tests) — but they run applications in isolated Docker containers on a single machine.
- In production, backends run on **Kubernetes clusters** with multi-replica deployments, shared resources, database topology decisions, and real network traffic.
- A functionally correct application can still **fail** in production if its deployment is poorly configured: wrong replica count, under-provisioned resources, bad placement, connection pool exhaustion, etc.
- **Performance optimization** (how to deploy for maximum throughput under fixed cluster resources) is a distinct and unsolved challenge that existing benchmarks do not address.

**Key sentence to write:** *"Generating correct code is a necessary but insufficient condition for production readiness — deployment configuration is a critical and largely unexamined dimension of LLM-based software generation."*

---

### §1.3 The gap: no systematic evaluation of LLM-driven deployment optimization

**State the gap explicitly:**

- BaxBench tests correctness and security but does not measure **runtime performance** under realistic deployment conditions.
- There is no benchmark that evaluates whether LLMs can iteratively **optimize deployment configurations** (replicas, resource allocation, database topology) based on performance feedback.
- Deployment optimization is currently a manual, expert-driven process — can LLMs reason about it?

**Cite related work where appropriate:**

- Kubernetes autoscaling (HPA, VPA) — reactive, not proactive reasoning.
- Cloud cost optimization tools — rule-based, not LLM-driven.
- LLM agents for DevOps (if relevant papers exist) — generally not evaluated with performance benchmarks.

---

### §1.4 This work: iterative deployment optimization on BaxBench + Kubernetes

**Present your contribution in one clear paragraph:**

- Extend BaxBench with a Kubernetes-based iterative benchmarking framework.
- LLM agent generates both application code **and** deployment configurations.
- An adaptive load testing pipeline measures sustainable throughput (**goodput**) under real cluster constraints.
- The agent receives structured performance feedback and iteratively refines either code or deployment over multiple rounds.
- Evaluate whether LLMs can autonomously improve deployment performance through this feedback loop.

---

### §1.5 Contributions

Numbered or bulleted list. Be concrete:

1. **An iterative benchmark framework** that extends BaxBench with Kubernetes deployment, adaptive load testing, and a multi-phase refinement loop (decision → code → spec → deploy → bench → feedback).

2. **A constrained deployment specification** (`spec.yaml`) that gives the LLM agent control over replica count, resource requests/limits, database topology, and placement — while the framework handles manifest rendering, validation, and orchestration.

3. **An adaptive load profile** (`k8s-goodput-plateau`) that automatically finds the sustainable throughput of any deployment without per-application RPS tuning.

4. **Experimental evaluation** on [N] BaxBench scenarios across [M] frameworks and [K] LLMs, showing [your main finding: e.g. "that iterative refinement improves goodput by X% on average" or "that deployment tuning contributes more than code optimization in Y% of cases"].

*(Adjust 4 to match your actual results — this is the one that requires your data.)*

---

### §1.6 Thesis outline

One paragraph or short list mapping chapters:

- **Chapter 2: Background** — BaxBench, Kubernetes, Locust, LLM code generation
- **Chapter 3: Methods** — System architecture, iterative loop, phase details, load profile
- **Chapter 4: Experimental setup** — Cluster configuration, models, scenarios, hyperparameters
- **Chapter 5: Results** — Goodput trajectories, code vs. deployment refinement analysis, case studies
- **Chapter 6: Discussion** — Limitations, threats to validity, comparison to baselines
- **Chapter 7: Conclusion** — Summary, future work

*(Adapt to your faculty's required chapter structure.)*

---

## What NOT to put in the introduction

- **Methods details** — save for Chapter 3. Intro only previews the approach.
- **Numbers/tables** — at most one headline result ("X% improvement"). Detailed data goes in Results.
- **Implementation specifics** — no code snippets, no CLI flags, no file paths.
- **Full related work survey** — intro cites a few key works to establish the gap; Chapter 2 (Background/Related Work) does the thorough survey.

---

## Comparison: your thesis vs. the two papers

| Aspect | BaxBench | AutoBaxBuilder | Your thesis |
|--------|----------|----------------|-------------|
| Focus | Correctness + security benchmark | Automated benchmark generation | **Performance + deployment optimization** |
| Evaluation target | Generated code quality | Generated tests/exploits quality | **Deployment throughput under K8s** |
| Runtime environment | Single Docker container | Single Docker container | **Multi-node Kubernetes cluster** |
| Feedback loop | None (single-shot generation) | Iterative test/exploit refinement | **Iterative deploy–bench–refine loop** |
| Agent controls | Only code | Scenarios + tests + exploits | **Code + deployment spec** |
| Primary metric | pass@1, sec_pass@1 | pass@1, sec_pass@1 | **Goodput (sustained successful RPS)** |

This table is useful in the intro or background to position your contribution clearly.

---

## Tone and framing advice

- **Don't frame as "BaxBench is bad"** — frame as "BaxBench addresses correctness and security; we extend this to the equally important but unstudied dimension of deployment performance."
- **Emphasize the practical relevance** — deployment misconfiguration is a real production problem; cloud cost optimization is a billion-dollar industry.
- **Be honest about scope** — this is a master's thesis, not a 50-author paper. State what you evaluate and what you leave to future work.
- **Use the same terminology consistently** — decide early whether you say "deployment optimization", "deployment tuning", or "infrastructure configuration" and stick with it.

---

## Example opening paragraph (draft)

> Large language models have demonstrated remarkable capabilities in generating
> functionally correct code, progressing from single-function synthesis to
> complete backend applications spanning multiple files and frameworks. Recent
> work by Vero et al. (2025) introduced BaxBench, a benchmark revealing that
> even state-of-the-art models produce security vulnerabilities in roughly half
> of their correct implementations. However, correctness and security, while
> necessary, are not sufficient for production readiness: a backend must also
> **perform** under realistic deployment conditions — with multiple replicas,
> constrained cluster resources, and real network traffic. This dimension of
> LLM-generated software remains largely unexamined. In this thesis, we extend
> BaxBench with a Kubernetes-based iterative benchmarking framework in which an
> LLM agent optimizes both application code and deployment configuration,
> guided by adaptive load testing feedback, to maximize sustainable throughput
> under fixed infrastructure constraints.

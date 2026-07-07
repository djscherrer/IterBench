# Master's Thesis — Write-up (Working Notes)

Working outlines for the thesis extending BaxBench with
Kubernetes-aware, iterative performance optimization.

These files are **draft structure and bullet points** for LaTeX — not
final prose. Copy sections into your thesis and expand with citations, figures,
and measured numbers from your experiments.

## Directory layout

```
Writeup/
├── README.md               ← this file
├── references.bib
├── introduction/
│   ├── 01-introduction-outline.md
│   └── 01-introduction-draft.tex
├── related_work/
│   ├── 01-paper-analysis.md      ← analysis of all 12 papers + layout
│   ├── 01-related-work-draft.tex ← full §2.1–§2.5 LaTeX draft
│   └── papers/                   ← PDFs (copy Borg, Glia, Decima here if missing)
└── methods/
    ├── 01-methods-chapter-outline.md
    ├── 01-methods-chapter-draft.tex   ← full Methods chapter LaTeX draft
    ├── 02-system-architecture.md
    ├── 03-iterative-experiment-loop.md
    ├── 04-phase-decision.md
    ├── 05-phase-code.md
    ├── 06-phase-spec.md
    ├── 07-phase-deploy.md
    ├── 08-phase-benchmark.md
    ├── 09-phase-outcome-feedback.md
    ├── 10-failure-handling.md
    ├── 11-design-principles.md
    ├── 12-figures-and-tables.md
    └── 13-load-profile-and-goodput.md
```

## Related repository documentation

- `docs/k8s_approach.md` — design rationale and CLI reference
- `docs/locust_pipeline.md` — load test and diagnostics layout
- `docs/k8s_conversational_prompt_slimming.md` — what context the agent sees per phase
- `src/k8s_bench/orchestration/execute.py` — canonical iteration pipeline

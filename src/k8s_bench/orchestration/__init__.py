"""
Orchestration layer for the k8s benchmark loop.

This package owns the *story* of one ``run_iterative_k8s_bench`` invocation:

- :mod:`config`     – frozen dataclasses for each scope (run / sample / iteration).
- :mod:`preflight`  – sample workspace setup; postlude cleanup.
- :mod:`plan`       – resolve iteration folder, skip checks, load prior signals.
- :mod:`execute`    – run all stages of one iteration in order.

The per-iteration stage work (decision, code, spec, bench, outcome) lives in
:mod:`k8s_bench.stages`.

Only the dataclasses are re-exported at package level; stage callers (e.g.
``loop.py``) import the orchestration helpers from their submodules to keep
``orchestration → stages → orchestration.config`` cycle-free.
"""

from __future__ import annotations

from .config import (
    IterationOutcome,
    IterationPlan,
    IterationSetup,
    PriorIteration,
    RefinementAction,
    RunConfig,
    SampleContext,
)

__all__ = [
    "IterationOutcome",
    "IterationPlan",
    "IterationSetup",
    "PriorIteration",
    "RefinementAction",
    "RunConfig",
    "SampleContext",
]

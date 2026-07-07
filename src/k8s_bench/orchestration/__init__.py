"""
Orchestration layer for the k8s benchmark loop.

This package owns the *story* of one iterative k8s experiment
(``loop._run_iterative_experiment_for_task``):

- :mod:`config`     – frozen dataclasses for each scope (run / sample / iteration).
- :mod:`preflight`  – sample workspace setup; postlude cleanup.
- :mod:`plan`       – resolve iteration folder, skip checks, load prior signals;
                      :func:`finalize_iteration_plan` builds :class:`IterationPlan`.
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
    RefinementAction,
    RunConfig,
    SampleContext,
)
from .lineage import IterationLineage, SpecRef

__all__ = [
    "IterationLineage",
    "IterationOutcome",
    "IterationPlan",
    "IterationSetup",
    "RefinementAction",
    "RunConfig",
    "SampleContext",
    "SpecRef",
]

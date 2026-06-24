"""
Per-iteration stages of the k8s benchmark loop.

Each stage takes ``(ctx, plan, cfg, logger)`` (plus stage-specific extras) and
performs one well-defined step in an iteration:

- :mod:`decision` – choose code vs spec path; rename iteration folder.
- :mod:`code`    – baseline codegen, code refinement, or copied lineage.
- :mod:`spec`    – produce ``spec.yaml`` (baseline / reuse / generate).
- :mod:`deploy`  – cluster deploy + readiness probe (``04-deploy/``).
- :mod:`bench`   – Locust load test against the deployed iteration.
- :mod:`outcome` – collect feedback, write artifacts, append summary block.
"""

from __future__ import annotations

from .bench import run_bench, run_locust_for_iteration
from .code import CodeStageResult, run_code_stage
from .decision import (
    RefinementDecision,
    decide_refinement_action,
    resolve_refinement_mode,
    run_decision_stage,
)
from .deploy import DeployProbeResult, DeployStageResult, probe_iteration_deployable, run_deploy_stage
from .outcome import record_outcome
from .spec import prepare_spec_or_fail

__all__ = [
    "CodeStageResult",
    "RefinementDecision",
    "DeployProbeResult",
    "DeployStageResult",
    "decide_refinement_action",
    "prepare_spec_or_fail",
    "probe_iteration_deployable",
    "record_outcome",
    "resolve_refinement_mode",
    "run_bench",
    "run_code_stage",
    "run_decision_stage",
    "run_deploy_stage",
    "run_locust_for_iteration",
]

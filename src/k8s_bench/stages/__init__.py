"""
Per-iteration stages of the k8s benchmark loop.

Each stage is invoked from :mod:`k8s_bench.orchestration.execute`, which creates
the per-phase logger and calls ``run_*_stage(ctx, plan, cfg, logger, ...)``:

- :mod:`decision` – choose code vs spec path; rename iteration folder.
- :mod:`code`    – baseline codegen, code refinement, or copied lineage.
- :mod:`spec`    – produce ``spec.yaml`` (baseline / reuse / generate).
- :mod:`deploy`  – cluster deploy + readiness probe (``04-deploy/``).
- :mod:`bench`   – Locust load test against the deployed iteration.
- :mod:`outcome` – collect feedback, write artifacts, append summary block.
"""

from __future__ import annotations

from .bench import run_bench_stage, run_locust_for_iteration
from .code import CodeStageResult, run_codegen_stage, run_code_lineage_stage
from .decision import (
    RefinementDecision,
    decide_refinement_action,
    resolve_refinement_mode,
    run_refinement_decision_stage,
)
from .deploy import DeployProbeResult, DeployStageResult, probe_iteration_deployable, run_deploy_stage
from .outcome import run_outcome_stage
from .spec import SpecStageResult, run_reuse_spec_stage, run_spec_generation_stage

__all__ = [
    "CodeStageResult",
    "RefinementDecision",
    "DeployProbeResult",
    "DeployStageResult",
    "SpecStageResult",
    "decide_refinement_action",
    "probe_iteration_deployable",
    "resolve_refinement_mode",
    "run_bench_stage",
    "run_codegen_stage",
    "run_code_lineage_stage",
    "run_refinement_decision_stage",
    "run_deploy_stage",
    "run_locust_for_iteration",
    "run_outcome_stage",
    "run_reuse_spec_stage",
    "run_spec_generation_stage",
]

"""
Prepare one iteration for execution: folder layout, skip checks, prior signals.

Refinement routing (baseline / forced / LLM) lives in
:mod:`k8s_bench.stages.decision`; :func:`finalize_iteration_plan` applies the
folder suffix and builds :class:`~k8s_bench.orchestration.config.IterationPlan`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workspace.skips import append_k8s_skip
from ..stages.decision import DecisionStageResult
from workspace import (
    apply_iteration_folder_suffix,
    ensure_iteration_core_layout,
    init_iteration_meta,
    is_baseline_iteration,
    resolve_iteration_dir,
    update_iteration_meta,
)
from .config import IterationPlan, IterationSetup, RunConfig, SampleContext
from .lineage import lineage_based_on_iteration_id, load_iteration_lineage


def plan_iteration(
    ctx: SampleContext,
    iteration_index: int,
    iteration_id: str,
    cfg: RunConfig,
) -> IterationSetup | None:
    """Resolve iteration folder, skip if already finished, load prior signals."""
    from workspace import find_finished_iteration_dir

    is_baseline = is_baseline_iteration(iteration_index)

    if not cfg.force:
        finished = find_finished_iteration_dir(
            ctx.sample_dir,
            iteration_id,
            experiment_id=ctx.experiment_id,
        )
        if finished is not None:
            append_k8s_skip(
                ctx.task_run_dir,
                ctx.sample,
                f"skipped iteration {iteration_id}: already finished "
                f"({finished.name}, load_profile={cfg.load_profile!r})",
            )
            return None

    iteration_path = _resolve_iteration_path(ctx, iteration_id)
    ensure_iteration_core_layout(iteration_path)

    lineage = load_iteration_lineage(
        ctx.sample_dir,
        iteration_index,
        is_baseline=is_baseline,
        experiment_id=ctx.experiment_id,
    )
    based_on = lineage_based_on_iteration_id(lineage)
    init_iteration_meta(
        iteration_path,
        iteration_index=iteration_index,
        iteration_id=iteration_id,
        based_on_iteration=based_on,
    )
    update_iteration_meta(iteration_path, refinement_mode=cfg.refinement_mode)

    return IterationSetup(
        iteration_id=iteration_id,
        iteration_index=iteration_index,
        iteration_path=iteration_path,
        lineage=lineage,
        is_baseline=is_baseline,
    )


def _resolve_iteration_path(ctx: SampleContext, iteration_id: str) -> Path:
    return resolve_iteration_dir(
        ctx.sample_dir,
        iteration_id,
        experiment_id=ctx.experiment_id,
    )


def finalize_iteration_plan(
    ctx: SampleContext,
    setup: IterationSetup,
    decision_result: DecisionStageResult,
) -> tuple[Path, IterationPlan]:
    """Apply folder suffix from the decision and build the per-iteration plan."""
    folder_kind = (
        "baseline"
        if decision_result.refinement_action == "baseline"
        else ("code" if decision_result.refinement_action == "code" else "spec")
    )
    old_section_id = setup.iteration_path.name
    iteration_path = apply_iteration_folder_suffix(setup.iteration_path, folder_kind)
    update_iteration_meta(iteration_path, folder=iteration_path.name)
    if iteration_path.name != old_section_id:
        try:
            from ..experiment_summary import rename_summary_iteration_section

            rename_summary_iteration_section(
                iteration_path=iteration_path,
                old_section_id=old_section_id,
                new_section_id=iteration_path.name,
            )
        except Exception:
            pass

    lineage = setup.lineage
    if decision_result.refinement_action == "deployment" and lineage.prior_code_dir is None:
        raise RuntimeError(
            f"No application code snapshot found for {setup.iteration_id} "
            "(deployment/spec refinement requires a prior non-empty "
            "`02-code/code/` under some earlier iteration)."
        )

    plan = IterationPlan(
        iteration_id=setup.iteration_id,
        iteration_index=setup.iteration_index,
        refinement_action=decision_result.refinement_action,
        decision=decision_result.decision,
        lineage=lineage,
    )
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    return iteration_path, plan


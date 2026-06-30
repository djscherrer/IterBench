"""
Prepare one iteration for execution: folder layout, skip checks, prior signals.

Decision (code vs spec) and folder suffix routing is handled by
``k8s_bench.orchestration.execute`` — not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..util.sample import append_k8s_skip
from ..workspace import (
    ensure_iteration_core_layout,
    init_iteration_meta,
    is_baseline_iteration,
    resolve_iteration_dir,
    update_iteration_meta,
)
from .config import IterationSetup, RunConfig, SampleContext
from .lineage import load_iteration_lineage


def plan_iteration(
    ctx: SampleContext,
    iteration_index: int,
    iteration_id: str,
    cfg: RunConfig,
) -> IterationSetup | None:
    """Resolve iteration folder, skip if already benched, load prior signals."""
    is_baseline = is_baseline_iteration(iteration_index)
    iteration_path = _resolve_iteration_path(ctx, iteration_id)
    ensure_iteration_core_layout(iteration_path)

    lineage = load_iteration_lineage(
        ctx.sample_dir,
        iteration_index,
        is_baseline=is_baseline,
        experiment_id=ctx.experiment_id,
    )
    based_on = (
        lineage.bench_feedback.iteration_id
        if lineage.bench_feedback is not None
        else None
    )
    init_iteration_meta(
        iteration_path,
        iteration_index=iteration_index,
        iteration_id=iteration_id,
        based_on_iteration=based_on,
    )
    update_iteration_meta(iteration_path, refinement_mode=cfg.refinement_mode)

    if not cfg.force and ctx.task.has_k8s_perf_run_for_iteration(
        ctx.sample_dir,
        iteration_id=iteration_id,
        load_profile=cfg.load_profile,
        experiment_id=ctx.experiment_id,
    ):
        append_k8s_skip(
            ctx.task_run_dir,
            ctx.sample,
            f"skipped iteration {iteration_id}: perf run already exists "
            f"(load_profile={cfg.load_profile!r})",
        )
        return None

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


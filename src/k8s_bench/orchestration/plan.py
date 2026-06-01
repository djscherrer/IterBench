"""
Build :class:`IterationPlan` for one iteration.

This is the *decision* phase that runs at the start of every iteration:

1. Resolve the iteration folder (creating it if needed) and write initial meta.
2. Skip if a perf run for this iteration already exists (and ``force`` is off).
3. Load prior signals from disk (``bench_feedback`` from earlier successful
   iterations; ``failure_report`` from prior code-failed iterations).
4. Decide ``refinement_action`` (baseline / code / deployment) — either forced
   by ``refinement_mode`` or by the decision LLM.
5. Rename the folder with the kind suffix (-baseline / -spec / -code).
6. Resolve explicit source artifacts (``reuse_spec_from``, ``source_code_dir``).

No other helper calls ``apply_iteration_folder_suffix``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..experiment_summary import append_refinement_decision_block
from ..feedback import load_prior_feedback_for_iteration
from ..refinement.code import find_latest_prior_failure_report
from ..refinement.decision import RefinementDecision, decide_refinement_action
from ..util.sample import append_k8s_skip
from ..workspace import (
    apply_iteration_folder_suffix,
    ensure_iteration_core_layout,
    init_iteration_meta,
    is_baseline_iteration,
    iteration_decision_log_path,
    latest_code_dir,
    resolve_iteration_dir,
    update_iteration_meta,
    write_decision,
)
from .config import (
    IterationPlan,
    PriorIteration,
    RefinementAction,
    RunConfig,
    SampleContext,
)


def plan_iteration(
    ctx: SampleContext,
    iteration_index: int,
    iteration_id: str,
    cfg: RunConfig,
) -> IterationPlan | None:
    """Build the :class:`IterationPlan` for one iteration, or ``None`` on skip."""
    is_baseline = is_baseline_iteration(iteration_index)
    iteration_path = _resolve_iteration_path(ctx, iteration_id)
    ensure_iteration_core_layout(iteration_path)

    refinement_action: RefinementAction = (
        "baseline" if is_baseline else "deployment"
    )
    prior = _load_prior(ctx, iteration_index, is_baseline=is_baseline)

    based_on = (
        prior.bench_feedback.iteration_id
        if prior.bench_feedback is not None
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
    ):
        append_k8s_skip(
            ctx.save_dir,
            ctx.sample,
            f"skipped iteration {iteration_id}: perf run already exists "
            f"(load_profile={cfg.load_profile!r})",
        )
        return None

    # All log output produced during the decision phase (LLM call, folder
    # rename, summary append) lands in ``01-decision/phase.log``. The handler
    # stays attached to the same inode across the ``apply_iteration_folder_suffix``
    # rename on Linux, so the file ends up at the renamed path.
    decision_log = iteration_decision_log_path(iteration_path)
    with ctx.task.create_logger(decision_log) as iteration_logger:
        decision = _decide_refinement(
            ctx,
            iteration_path,
            iteration_index,
            iteration_id,
            cfg,
            prior,
            is_baseline,
            iteration_logger,
        )
        if decision is not None:
            refinement_action = (
                decision.action if decision.action != "deployment" else "deployment"
            )
            if decision.action == "code":
                refinement_action = "code"

        iteration_path = apply_iteration_folder_suffix(
            iteration_path, _folder_kind(refinement_action)
        )
        update_iteration_meta(iteration_path, folder=iteration_path.name)

        baseline_code = ctx.task.get_code_dir(ctx.results_dir, ctx.sample)
        source_code_dir = latest_code_dir(ctx.sample_dir, fallback=baseline_code)

        reuse_spec_from: str | None = None
        if refinement_action == "code" and prior.bench_feedback is not None:
            reuse_spec_from = prior.bench_feedback.iteration_id

        if refinement_action == "code":
            # Re-load with failure_report attached so executors can render the
            # prior FT failure as a dedicated prompt block.
            failure_report = find_latest_prior_failure_report(
                ctx.sample_dir, current_iteration_index=iteration_index
            )
            prior = PriorIteration(
                bench_feedback=prior.bench_feedback,
                failure_report=failure_report,
            )
            if failure_report is not None:
                iteration_logger.info(
                    "iteration %s: prior code-refinement failure detected in %s "
                    "(%d/%d FT passed, failed=%s); will surface in prompt",
                    iteration_id,
                    failure_report.iteration_id,
                    failure_report.num_passed_ft,
                    failure_report.num_total_ft,
                    [ft.name for ft in failure_report.failed_tests] or "(unknown)",
                )

    return IterationPlan(
        iteration_id=iteration_id,
        iteration_index=iteration_index,
        refinement_action=refinement_action,
        decision=decision,
        prior=prior,
        reuse_spec_from=reuse_spec_from,
        source_code_dir=source_code_dir,
    )


def _resolve_iteration_path(ctx: SampleContext, iteration_id: str) -> Path:
    iteration_path = resolve_iteration_dir(ctx.sample_dir, iteration_id)
    if not iteration_path.is_dir() and not (iteration_path / "meta.json").is_file():
        iteration_path = ctx.task.get_k8s_iteration_dir(
            ctx.results_dir, ctx.sample, iteration_id
        )
    return iteration_path


def _load_prior(
    ctx: SampleContext,
    iteration_index: int,
    *,
    is_baseline: bool,
) -> PriorIteration:
    bench_feedback = None
    if not is_baseline:
        bench_feedback = load_prior_feedback_for_iteration(
            ctx.sample_dir, iteration_index
        )
    return PriorIteration(bench_feedback=bench_feedback, failure_report=None)


def _folder_kind(action: RefinementAction) -> str:
    if action == "baseline":
        return "baseline"
    return "code" if action == "code" else "spec"


def _decide_refinement(
    ctx: SampleContext,
    iteration_path: Path,
    iteration_index: int,
    iteration_id: str,
    cfg: RunConfig,
    prior: PriorIteration,
    is_baseline: bool,
    logger: logging.Logger,
) -> RefinementDecision | None:
    """Run/force the refinement decision and persist it to disk."""
    if is_baseline or cfg.refinement_mode == "off":
        return None

    if prior.bench_feedback is None:
        # No benchmark feedback yet: default to deployment without an LLM call.
        logger.warning(
            "iteration %s: no benchmark feedback from prior iterations; "
            "defaulting to spec tuning (%s mode)",
            iteration_id,
            cfg.refinement_mode,
        )
        decision = RefinementDecision(
            action="deployment",
            rationale=(
                "No benchmark feedback from prior iterations; "
                "defaulting to deployment/spec tuning."
            ),
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration="",
        )
        write_decision(iteration_path, decision)
        update_iteration_meta(iteration_path, refinement_action="deployment")
        return decision

    if cfg.refinement_mode == "code":
        decision = RefinementDecision(
            action="code",
            rationale=f"Forced by refinement mode={cfg.refinement_mode!r}",
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration=prior.bench_feedback.iteration_id,
        )
    elif cfg.refinement_mode == "deployment":
        decision = RefinementDecision(
            action="deployment",
            rationale=f"Forced by refinement mode={cfg.refinement_mode!r}",
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration=prior.bench_feedback.iteration_id,
        )
    else:
        decision = decide_refinement_action(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            prior_feedback=prior.bench_feedback,
            iteration_index=iteration_index,
            next_iteration_id=iteration_id,
            logger=logger,
            vllm_port=cfg.vllm_port,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            total_iterations=cfg.total_iterations,
        )

    write_decision(iteration_path, decision)
    update_iteration_meta(
        iteration_path,
        refinement_action=decision.action,
        based_on_iteration=prior.bench_feedback.iteration_id,
    )
    try:
        append_refinement_decision_block(
            sample_dir=ctx.sample_dir,
            iteration_id=iteration_id,
            decision=decision,
            load_profile=cfg.load_profile,
        )
    except Exception as exc:
        logger.warning(
            "Could not update experiment summary (decision): %s", exc
        )

    if decision.action == "code":
        logger.info(
            "iteration %s: will refine application code after folder setup",
            iteration_id,
        )
    return decision

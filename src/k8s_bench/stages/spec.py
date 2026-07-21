"""
Spec preparation stage (``03-spec/``).

Orchestrates retry loops, reuse, and failure handling. Writes and validates
``spec.yaml`` only; manifest rendering happens in the deploy stage.
Single attempts live in :mod:`k8s_bench.spec.attempt` (``run_spec_attempt``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..cluster.capacity import collect_cluster_capacity
from ..failure import (
    SpecFailureRecord,
    build_spec_iteration_failure,
    fail_iteration_phase,
)
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..orchestration.lineage import prior_spec_failure_record
from ..spec.attempt import SpecAttemptResult, run_spec_attempt
from ..spec.reuse import reuse_deployment_spec_for_iteration
from workspace import (
    find_iteration_spec_path,
    resolve_iteration_dir,
    update_iteration_meta,
)


@dataclass(frozen=True)
class SpecStageResult:
    spec_file: Path | None
    abort_sample: bool = False


def run_spec_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
    *,
    mode: str,
    max_attempts: int = 1,
) -> SpecStageResult:
    """
    Generate a new ``spec.yaml`` for this iteration (``03-spec``).

    Static spec validation only; cluster deploy runs in ``04-deploy``.
    """
    del image_id  # image is patched during deploy, not spec generation

    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    is_baseline = mode == "baseline"
    enable_attempts = is_baseline or max_attempts > 1

    prior_spec_failure = prior_spec_failure_record(plan.lineage)
    validation_feedback: str | None = (
        prior_spec_failure.to_prompt_block()
        if (not is_baseline and prior_spec_failure is not None)
        else None
    )

    capacity = collect_cluster_capacity()
    last_validation_errors: tuple[str, ...] = ()
    last_validation_warnings: tuple[str, ...] = ()
    last_err = (
        "baseline spec generation did not produce a valid configuration"
        if is_baseline
        else "static spec validation failed"
    )
    spec_file: Path | None = None
    label = "Baseline" if is_baseline else "Refinement"

    for attempt in range(1, max_attempts + 1):
        if enable_attempts:
            try:
                from ..failure.persist import load_prior_spec_attempt_failure

                prev = load_prior_spec_attempt_failure(iteration_path, attempt)
                if prev is not None:
                    validation_feedback = prev.to_prompt_block()
            except Exception:
                pass
        if ctx.session is None:
            raise RuntimeError(
                "missing LLM session on SampleContext; expected sample_preflight() to "
                "initialize ctx.session for iterative experiments"
            )
        logger.info(
            "%s spec attempt %d/%d for sample %d",
            label,
            attempt,
            max_attempts,
            ctx.sample,
        )
        result = run_spec_attempt(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            iteration_id=plan.iteration_id,
            session=ctx.session,
            logger=logger,
            capacity=capacity,
            refinement=not is_baseline,
            validation_feedback=validation_feedback,
            max_validation_retries=3 if is_baseline else 1,
            attempt_index=attempt,
            enable_attempts=enable_attempts,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
            experiment_id=ctx.experiment_id,
            llm_max_cost_usd=ctx.llm_max_cost_usd,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
        )
        if result.spec_path is None:
            last_err = result.error or last_err
            if result.failure is not None:
                validation_feedback = SpecFailureRecord(
                    phase="spec",
                    kind=result.failure.kind,
                    iteration_id=result.failure.iteration_id or plan.iteration_id,
                    attempt=attempt,
                    summary=result.failure.summary,
                    errors=result.failure.errors,
                    warnings=result.failure.warnings,
                    llm_error=result.failure.llm_error,
                ).to_prompt_block()
            else:
                validation_feedback = result.error
            last_validation_errors = getattr(result, "validation_errors", ()) or ()
            last_validation_warnings = getattr(result, "validation_warnings", ()) or ()
            if attempt < max_attempts:
                logger.warning(
                    "spec generation attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    last_err,
                )
            continue

        spec_file = result.spec_path
        _append_spec_summary_on_success(
            ctx=ctx,
            plan=plan,
            iteration_path=iteration_path,
            result=result,
            logger=logger,
        )
        break

    if spec_file is None:
        fallback = SpecFailureRecord(
            phase="spec",
            kind="spec_validation",
            iteration_id=plan.iteration_id,
            summary=last_err,
            errors=last_validation_errors or (validation_feedback or last_err,),
            warnings=last_validation_warnings,
        )
        iteration_failure = build_spec_iteration_failure(
            iteration_path,
            iteration_id=plan.iteration_id,
            terminal_attempt=max_attempts,
            fallback=fallback,
            logger=logger,
        )
        fail_iteration_phase(
            iteration_path=iteration_path,
            task_run_dir=ctx.task_run_dir,
            sample_dir=ctx.sample_dir,
            sample=ctx.sample,
            iteration_id=plan.iteration_id,
            kind="spec",
            logger=logger,
            iteration_failure=iteration_failure,
        )
        return SpecStageResult(None, abort_sample=is_baseline)

    update_iteration_meta(iteration_path, spec_regenerated=True)

    spec_file = spec_file or find_iteration_spec_path(iteration_path)
    if spec_file is None:
        fail_iteration_phase(
            iteration_path=iteration_path,
            task_run_dir=ctx.task_run_dir,
            sample_dir=ctx.sample_dir,
            sample=ctx.sample,
            iteration_id=plan.iteration_id,
            failure_reason="no spec.yaml after prepare",
            kind="spec",
            logger=logger,
        )
        return SpecStageResult(None, abort_sample=is_baseline)

    return SpecStageResult(spec_file)


def _append_spec_summary_on_success(
    *,
    ctx: SampleContext,
    plan: IterationPlan,
    iteration_path: Path,
    result: SpecAttemptResult,
    logger: logging.Logger,
) -> None:
    if result.spec is None or result.spec_path is None:
        return
    try:
        from ..experiment_summary import append_spec_generation_block

        append_spec_generation_block(
            sample_dir=ctx.sample_dir,
            iteration_id=plan.iteration_id,
            iteration_path=iteration_path,
            spec=result.spec,
            warnings=list(result.warnings),
            errors=list(result.validation_errors),
            had_prior_feedback=plan.iteration_index > 0,
            iteration_index=plan.iteration_index,
        )
    except Exception as exc:
        logger.warning("Could not update experiment summary: %s", exc)


def run_reuse_spec_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    *,
    source_iteration_id: str,
    logger: logging.Logger,
) -> SpecStageResult:
    """Reuse a prior spec.yaml (code refinement path)."""
    from tasks import esc

    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    bench_labels_dict = {
        "baxbench.dev/model": esc(ctx.task.model),
        "baxbench.dev/scenario": esc(ctx.task.scenario.id),
        "baxbench.dev/env": esc(ctx.task.env.id),
        "baxbench.dev/spec-gen": "true",
        "baxbench.dev/phase": str(plan.iteration_index),
    }
    logger.info(
        "iteration %s: reusing deployment spec from %s (code refinement; no LLM spec generation)",
        plan.iteration_id,
        source_iteration_id,
    )
    spec_file = reuse_deployment_spec_for_iteration(
        iteration_path=iteration_path,
        sample_dir=ctx.sample_dir,
        source_iteration_id=source_iteration_id,
        target_iteration_id=plan.iteration_id,
        extra_labels=bench_labels_dict,
        logger=logger,
        experiment_id=ctx.experiment_id,
    )
    update_iteration_meta(
        iteration_path,
        spec_regenerated=False,
        spec_reused_from=source_iteration_id,
    )
    spec_file = spec_file or find_iteration_spec_path(iteration_path)
    if spec_file is None:
        fail_iteration_phase(
            iteration_path=iteration_path,
            task_run_dir=ctx.task_run_dir,
            sample_dir=ctx.sample_dir,
            sample=ctx.sample,
            iteration_id=plan.iteration_id,
            failure_reason="no spec.yaml after reuse",
            kind="spec",
            logger=logger,
        )
        return SpecStageResult(None)
    return SpecStageResult(spec_file)

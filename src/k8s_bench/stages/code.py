"""
Code stage (``02-code/``): baseline codegen, refinement, or copied lineage.

Orchestrates retry loops and failure handling. Single attempts live in
:mod:`k8s_bench.code.attempt` (``run_code_attempt``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..code.attempt import (
    CodegenMode,
    prepare_codegen_workspace,
    run_code_attempt,
)
from ..code.baseline_meta import (
    append_baseline_summary,
    try_reuse_baseline_codegen,
    write_codegen_meta,
)
from ..failure import build_code_iteration_failure, fail_iteration_phase
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..orchestration.lineage import prior_code_failure_record
from workspace import (
    iteration_code_attempts_dir,
    iteration_code_phase_dir,
    iteration_id_for_index,
    materialize_code_lineage,
    next_attempt_index,
    resolve_iteration_dir,
    update_iteration_meta,
)


@dataclass(frozen=True)
class CodeStageResult:
    image_id: str | None
    abort_sample: bool = False


def run_code_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    cfg: RunConfig,
    logger: logging.Logger,
    *,
    mode: CodegenMode,
    max_attempts: int,
    abort_sample_on_fail: bool,
) -> CodeStageResult:
    """
    Run code generation + functional-test validation (``02-code``).

    Used for both baseline and refinement by passing ``mode`` and ``max_attempts``.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )

    if mode == "baseline" and iteration_path.is_dir() and not cfg.force:
        reused = try_reuse_baseline_codegen(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
        )
        if reused is not None:
            _code_dir, image_id = reused
            return CodeStageResult(image_id)

    phase_dir = prepare_codegen_workspace(
        mode=mode,
        iteration_path=iteration_path,
        force=cfg.force,
    )
    if ctx.session is None:
        raise RuntimeError(
            "missing LLM session on SampleContext; expected sample_preflight() to "
            "initialize ctx.session for iterative experiments"
        )

    is_baseline = mode == "baseline"
    # Track per-attempt dirs whenever we have retries (not only baseline).
    enable_attempts = is_baseline or max_attempts > 1
    fail_fast_on_infra = is_baseline
    log_label = "Baseline codegen" if is_baseline else "Code refinement"

    last_error: str | None = None
    terminal_infra_failure = False
    image_id: str | None = None

    for attempt_idx in range(1, max_attempts + 1):
        logger.info(
            "%s attempt %d/%d for sample %d (iteration=%s)",
            log_label,
            attempt_idx,
            max_attempts,
            ctx.sample,
            iteration_path.name,
        )

        result = run_code_attempt(
            mode=mode,
            attempt_index=attempt_idx,
            max_attempts=max_attempts,
            prompter=ctx.session,
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            sample_dir=ctx.sample_dir,
            iteration_path=iteration_path,
            phase_dir=phase_dir,
            logger=logger,
            ft_timeout=cfg.ft_timeout,
            num_ports=cfg.num_ports,
            min_port=cfg.min_port,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            prior_iteration_failure=prior_code_failure_record(plan.lineage),
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
            enable_attempts=enable_attempts,
            fail_fast_on_infra=fail_fast_on_infra,
            experiment_id=ctx.experiment_id,
            llm_max_cost_usd=cfg.llm_max_cost_usd,
        )

        if result.passed and result.image_id is not None:
            image_id = result.image_id
            if is_baseline:
                write_codegen_meta(
                    iteration_path,
                    status="passed",
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    task=ctx.task,
                    winning_attempt=attempt_idx,
                )
                append_baseline_summary(
                    sample_dir=ctx.sample_dir,
                    iteration_path=iteration_path,
                    task=ctx.task,
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    winning_attempt=attempt_idx,
                    status="passed",
                    error=None,
                    logger=logger,
                )
            break

        last_error = result.error
        if result.infra_failure:
            terminal_infra_failure = True

        if not result.continue_loop:
            break

        if attempt_idx < max_attempts and last_error:
            logger.warning(
                "%s attempt %d/%d will retry after: %s",
                log_label,
                attempt_idx,
                max_attempts,
                last_error,
            )

    if image_id is None:
        if is_baseline:
            _finalize_baseline_codegen_failure(
                task=ctx.task,
                sample=ctx.sample,
                sample_dir=ctx.sample_dir,
                task_run_dir=ctx.task_run_dir,
                iteration_path=iteration_path,
                max_attempts=max_attempts,
                last_error=last_error,
                terminal_infra_failure=terminal_infra_failure,
                logger=logger,
            )
        if mode == "refinement":
            terminal_attempt = next_attempt_index(
                iteration_code_attempts_dir(iteration_path)
            ) - 1
            if terminal_attempt < 1:
                terminal_attempt = max_attempts
            iteration_failure = build_code_iteration_failure(
                iteration_path,
                iteration_id=plan.iteration_id,
                terminal_attempt=terminal_attempt,
                logger=logger,
            )
            fail_iteration_phase(
                iteration_path=iteration_path,
                task_run_dir=ctx.task_run_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=plan.iteration_id,
                kind="code",
                logger=logger,
                iteration_failure=iteration_failure,
            )
        return CodeStageResult(None, abort_sample=abort_sample_on_fail)

    if mode == "refinement":
        update_iteration_meta(iteration_path, code_modified=True)
        try:
            from ..experiment_summary import append_code_refinement_block

            append_code_refinement_block(
                sample_dir=ctx.sample_dir,
                iteration_id=plan.iteration_id,
                iteration_path=iteration_path,
            )
        except Exception as exc:
            logger.warning("Could not update experiment summary (code refinement): %s", exc)

    return CodeStageResult(image_id)


def _finalize_baseline_codegen_failure(
    *,
    task: Any,
    sample: int,
    sample_dir: Path,
    task_run_dir: Path | None,
    iteration_path: Path,
    max_attempts: int,
    last_error: str | None,
    terminal_infra_failure: bool,
    logger: logging.Logger,
) -> None:
    attempts_used = next_attempt_index(iteration_code_attempts_dir(iteration_path)) - 1
    if attempts_used < 1:
        attempts_used = max_attempts
    terminal_status = "infra_failed" if terminal_infra_failure else "failed"
    write_codegen_meta(
        iteration_path,
        status=terminal_status,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        task=task,
        winning_attempt=None,
        error=last_error,
        infra_failure=terminal_infra_failure,
    )
    append_baseline_summary(
        sample_dir=sample_dir,
        iteration_path=iteration_path,
        task=task,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        winning_attempt=None,
        status=terminal_status,
        error=last_error,
        logger=logger,
    )
    iteration_failure = build_code_iteration_failure(
        iteration_path,
        iteration_id=iteration_id_for_index(0),
        terminal_attempt=attempts_used,
        logger=logger,
    )
    if task_run_dir is None:
        return
    fail_iteration_phase(
        iteration_path=iteration_path,
        task_run_dir=task_run_dir,
        sample_dir=sample_dir,
        sample=sample,
        iteration_id=iteration_id_for_index(0),
        kind="code",
        logger=logger,
        iteration_failure=iteration_failure,
    )


def run_reuse_code_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    logger: logging.Logger,
) -> CodeStageResult:
    """Reuse prior application code without LLM (deployment/spec refinement path)."""
    if plan.lineage.prior_code_dir is None:
        raise RuntimeError(
            f"iteration {plan.iteration_id}: no prior_code_dir for code lineage copy"
        )
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    iteration_code_phase_dir(iteration_path).mkdir(parents=True, exist_ok=True)
    image_id = (
        materialize_code_lineage(
            iteration_path,
            plan.lineage.prior_code_dir,
            fallback_image_id=ctx.base_image_id,
        )
        or ctx.base_image_id
    )
    logger.info(
        "iteration %s: copied code lineage from %s (image=%s)",
        plan.iteration_id,
        plan.lineage.prior_code_dir,
        image_id,
    )
    return CodeStageResult(image_id)

"""
Code stage (``02-code/``): baseline codegen, refinement, or copied lineage.

This stage owns retry loops and failure handling. Single-attempt helpers live in
:mod:`k8s_bench.code.generation` (``generate_and_validate_code``, etc.).

- **baseline** — multi-attempt LLM codegen + FT validation; failure aborts sample.
- **code** — same loop in refinement mode; failure marks ``-code-failed``.
- **deployment** — copy prior code + FT artifacts (no LLM).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..code.baseline_meta import try_reuse_baseline_codegen
from ..code.generation import (
    CodegenMode,
    CodegenRetryState,
    finalize_baseline_codegen_failure,
    generate_and_validate_code,
    prepare_codegen_workspace,
    record_baseline_codegen_success,
)
from ..failure import fail_iteration_phase
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import (
    iteration_code_phase_dir,
    materialize_code_lineage,
    resolve_iteration_dir,
    update_iteration_meta,
)


@dataclass(frozen=True)
class CodeStageResult:
    image_id: str | None
    abort_sample: bool = False


def run_codegen_stage(
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

    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)

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

    if mode == "refinement" and (ctx.task.use_openhands or ctx.task.use_claude_agent):
        logger.warning(
            "Code refinement uses single-prompt Prompter; agent modes are not "
            "supported for k8s code refinement yet"
        )

    phase_dir = prepare_codegen_workspace(
        mode=mode,
        iteration_path=iteration_path,
        force=cfg.force,
    )

    image_id = _codegen_with_retries(
        ctx=ctx,
        plan=plan,
        cfg=cfg,
        iteration_path=iteration_path,
        phase_dir=phase_dir,
        logger=logger,
        mode=mode,
        max_attempts=max_attempts,
    )

    if image_id is None:
        if mode == "refinement":
            fail_iteration_phase(
                iteration_path=iteration_path,
                task_run_dir=ctx.task_run_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=plan.iteration_id,
                failure_reason=(
                    "Functional tests did not pass after code refinement"
                ),
                kind="code",
                logger=logger,
            )
        return CodeStageResult(None, abort_sample=abort_sample_on_fail)

    if mode == "refinement":
        update_iteration_meta(iteration_path, code_modified=True)

    return CodeStageResult(image_id)


def _codegen_with_retries(
    *,
    ctx: SampleContext,
    plan: IterationPlan,
    cfg: RunConfig,
    iteration_path: Path,
    phase_dir: Path,
    logger: logging.Logger,
    mode: CodegenMode,
    max_attempts: int,
) -> str | None:
    """Retry ``generate_and_validate_code`` until FTs pass or attempts exhaust."""
    is_baseline = mode == "baseline"
    track_attempt_dirs = is_baseline
    fail_fast_on_infra = is_baseline
    log_label = "Baseline codegen" if is_baseline else "Code refinement"

    retry_state = CodegenRetryState()
    last_error: str | None = None
    terminal_infra_failure = False

    for attempt_idx in range(1, max_attempts + 1):
        logger.info(
            "%s attempt %d/%d for sample %d (iteration=%s)",
            log_label,
            attempt_idx,
            max_attempts,
            ctx.sample,
            iteration_path.name,
        )

        result = generate_and_validate_code(
            mode=mode,
            attempt_index=attempt_idx,
            max_attempts=max_attempts,
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
            vllm_port=cfg.vllm_port,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            prior_feedback=plan.lineage.bench_feedback,
            prior_failure_report=plan.lineage.code_failure_report,
            retry_state=retry_state,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
            track_attempt_dirs=track_attempt_dirs,
            fail_fast_on_infra=fail_fast_on_infra,
            experiment_id=ctx.experiment_id,
            llm_max_cost_usd=ctx.llm_max_cost_usd,
        )

        if result.passed and result.image_id is not None:
            if is_baseline:
                record_baseline_codegen_success(
                    iteration_path=iteration_path,
                    sample_dir=ctx.sample_dir,
                    task=ctx.task,
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    winning_attempt=attempt_idx,
                    logger=logger,
                )
            return result.image_id

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

    if is_baseline:
        finalize_baseline_codegen_failure(
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

    return None


def run_code_lineage_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    logger: logging.Logger,
) -> CodeStageResult:
    """Materialize code lineage without LLM (deployment/spec refinement path)."""
    if plan.lineage.latest_code_dir is None:
        raise RuntimeError(
            f"iteration {plan.iteration_id}: no latest_code_dir for code lineage copy"
        )
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    iteration_code_phase_dir(iteration_path).mkdir(parents=True, exist_ok=True)
    image_id = (
        materialize_code_lineage(
            iteration_path,
            plan.lineage.latest_code_dir,
            fallback_image_id=ctx.base_image_id,
        )
        or ctx.base_image_id
    )
    logger.info(
        "iteration %s: copied code lineage from %s (image=%s)",
        plan.iteration_id,
        plan.lineage.latest_code_dir,
        image_id,
    )
    return CodeStageResult(image_id)

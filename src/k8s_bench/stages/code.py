"""
Code stage (``02-code/``): baseline codegen, refinement, or copied lineage.

Orchestrates the three refinement-action paths:

- **baseline** — unified LLM codegen loop (``code.generation``) with multi-attempt
  FT validation; failure aborts the sample.
- **code** — same codegen loop in refinement mode; failure marks ``-code-failed``.
- **deployment** — copy prior code + FT artifacts (no LLM).
"""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

from ..code.generation import CodegenMode, run_codegen_until_passing
from ..iteration_failure import fail_iteration_phase
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import (
    iteration_code_log_path,
    iteration_code_phase_dir,
    materialize_code_lineage,
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
) -> CodeStageResult:
    """Run ``02-code`` for this iteration; return docker image id or failure."""
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)

    if plan.refinement_action == "baseline":
        return _run_codegen_branch(
            ctx,
            plan,
            cfg,
            iteration_path=iteration_path,
            mode="baseline",
            max_attempts=cfg.baseline_code_max_attempts,
            abort_sample_on_fail=True,
        )

    if plan.refinement_action == "deployment":
        image_id = (
            materialize_code_lineage(
                iteration_path,
                plan.source_code_dir,
                fallback_image_id=ctx.base_image_id,
            )
            or ctx.base_image_id
        )
        return CodeStageResult(image_id)

    if (
        plan.refinement_action == "code"
        and plan.prior.bench_feedback is not None
    ):
        return _run_codegen_branch(
            ctx,
            plan,
            cfg,
            iteration_path=iteration_path,
            mode="refinement",
            max_attempts=1,
            abort_sample_on_fail=False,
        )

    return CodeStageResult(ctx.base_image_id)


def _run_codegen_branch(
    ctx: SampleContext,
    plan: IterationPlan,
    cfg: RunConfig,
    *,
    iteration_path: Path,
    mode: CodegenMode,
    max_attempts: int,
    abort_sample_on_fail: bool,
) -> CodeStageResult:
    iteration_code_phase_dir(iteration_path).mkdir(parents=True, exist_ok=True)
    code_log = iteration_code_log_path(iteration_path)

    with ctx.task.create_logger(code_log) as logger:
        outcome = run_codegen_until_passing(
            mode=mode,
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            sample_dir=ctx.sample_dir,
            iteration_path=iteration_path,
            logger=logger,
            ft_timeout=cfg.ft_timeout,
            num_ports=cfg.num_ports,
            min_port=cfg.min_port,
            vllm_port=cfg.vllm_port,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            max_attempts=max_attempts,
            force=cfg.force,
            task_run_dir=ctx.task_run_dir if mode == "baseline" else None,
            prior_feedback=plan.prior.bench_feedback,
            prior_failure_report=plan.prior.failure_report,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
        )

        if not outcome.ok or outcome.image_id is None:
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

        return CodeStageResult(outcome.image_id)

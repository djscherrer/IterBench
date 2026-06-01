"""
Code refinement stage.

When :class:`IterationPlan.refinement_action` is ``"code"``, this stage calls
the code-refinement LLM, snapshots the new code under the iteration directory,
runs the functional tests, and returns the new docker image id. On failure it
calls :func:`fail_iteration_phase` (which renames the folder to
``-code-failed`` and persists ``failure_report.json``) and returns ``None``.
"""

from __future__ import annotations

from ..iteration_failure import fail_iteration_phase
from ..refinement.code import refine_code_until_passing
from ..workspace import (
    iteration_code_log_path,
    iteration_code_phase_dir,
    resolve_iteration_dir,
    update_iteration_meta,
)
from ..orchestration.config import IterationPlan, RunConfig, SampleContext


def refine_code_or_fail(
    ctx: SampleContext,
    plan: IterationPlan,
    cfg: RunConfig,
) -> str | None:
    """Run code refinement; return new image id or ``None`` on FT failure.

    Opens ``02-code/phase.log`` so all code-refinement narrative (LLM call,
    snapshotting, FT outcomes) lands next to the LLM transcript + failure
    report, instead of leaking into the bench log.
    """
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    iteration_code_phase_dir(iteration_path).mkdir(parents=True, exist_ok=True)
    code_log = iteration_code_log_path(iteration_path)
    with ctx.task.create_logger(code_log) as logger:
        new_image = refine_code_until_passing(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            prior_feedback=plan.prior.bench_feedback,
            logger=logger,
            ft_timeout=cfg.ft_timeout,
            num_ports=cfg.num_ports,
            min_port=cfg.min_port,
            vllm_port=cfg.vllm_port,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            max_codegen_attempts=1,
            prior_failure_report=plan.prior.failure_report,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
        )
        if new_image is None:
            fail_iteration_phase(
                iteration_path=iteration_path,
                save_dir=ctx.save_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=plan.iteration_id,
                failure_reason="Functional tests did not pass after code refinement",
                kind="code",
                logger=logger,
            )
            return None

    update_iteration_meta(iteration_path, code_modified=True)
    return new_image

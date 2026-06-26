"""
Spec preparation stage (``03-spec/``).

Produces ``spec.yaml`` via LLM + static validation. This stage owns the retry
loop and failure handling; :mod:`k8s_bench.spec.generation` provides single-attempt
helpers (``generate_and_write_spec``, ``reuse_deployment_spec_for_iteration``).

- **baseline**: multiple attempts; each successful static validation is followed
  by a deploy probe (cluster must accept the spec before the stage succeeds).
- **refinement**: multiple attempts with static validation only; deploy is
  handled later by :mod:`stages.deploy`.
- **reuse**: copy ``spec.yaml`` from a prior iteration (code-refinement path).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..cluster.capacity import collect_cluster_capacity
from ..failure import fail_iteration_phase
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..spec.generation import (
    generate_and_write_spec,
    record_spec_deploy_probe_failure,
    reuse_deployment_spec_for_iteration,
)
from ..workspace import (
    find_iteration_spec_path,
    resolve_iteration_dir,
    update_iteration_meta,
)
from .deploy import DeployProbeResult, baseline_deploy_probe_callback


@dataclass(frozen=True)
class SpecStageResult:
    spec_file: Path | None
    abort_sample: bool = False


def run_spec_generation_stage(
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

    - ``mode="baseline"``: static validation + deploy probe per attempt.
    - ``mode="refinement"``: static validation only (deploy stage runs later).
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    is_baseline = mode == "baseline"

    deploy_probe: Callable[[], DeployProbeResult] | None = None
    if is_baseline:
        deploy_probe = baseline_deploy_probe_callback(
            ctx, plan, image_id, cfg, iteration_path, logger
        )

    spec_file = _generate_spec_with_retries(
        ctx=ctx,
        plan=plan,
        cfg=cfg,
        iteration_path=iteration_path,
        logger=logger,
        max_attempts=max_attempts,
        is_baseline=is_baseline,
        deploy_probe=deploy_probe,
    )

    if spec_file is None:
        return SpecStageResult(
            None,
            abort_sample=is_baseline,
        )

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
            kind="spec" if not is_baseline else "baseline",
            logger=logger,
        )
        return SpecStageResult(None, abort_sample=is_baseline)

    return SpecStageResult(spec_file)


def _generate_spec_with_retries(
    *,
    ctx: SampleContext,
    plan: IterationPlan,
    cfg: RunConfig,
    iteration_path: Path,
    logger: logging.Logger,
    max_attempts: int,
    is_baseline: bool,
    deploy_probe: Callable[[], DeployProbeResult] | None,
) -> Path | None:
    """Retry ``generate_and_write_spec`` until success or attempts exhausted."""
    capacity = collect_cluster_capacity()
    validation_feedback: str | None = None
    last_err = (
        "baseline spec generation did not produce a deployable configuration"
        if is_baseline
        else "static spec validation failed"
    )
    spec_file: Path | None = None
    label = "Baseline" if is_baseline else "Refinement"

    for attempt in range(1, max_attempts + 1):
        logger.info(
            "%s spec attempt %d/%d for sample %d",
            label,
            attempt,
            max_attempts,
            ctx.sample,
        )
        spec_file, gen_err = generate_and_write_spec(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            iteration_id=plan.iteration_id,
            logger=logger,
            capacity=capacity,
            prior_feedback=plan.lineage.bench_feedback if not is_baseline else None,
            validation_feedback=validation_feedback,
            max_validation_retries=3 if is_baseline else 1,
            enable_attempts=is_baseline,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
            vllm_port=cfg.vllm_port,
            experiment_id=ctx.experiment_id,
            llm_max_cost_usd=ctx.llm_max_cost_usd,
        )
        if spec_file is None:
            last_err = gen_err or last_err
            validation_feedback = gen_err
            if attempt < max_attempts:
                logger.warning(
                    "spec generation attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    last_err,
                )
            continue

        if deploy_probe is not None:
            probe = deploy_probe()
            if probe.ok:
                logger.info(
                    "baseline deploy probe passed on attempt %d for sample %d",
                    attempt,
                    ctx.sample,
                )
                return spec_file

            last_err = probe.reason
            validation_feedback = probe.to_prompt_feedback()
            record_spec_deploy_probe_failure(
                iteration_path,
                probe_reason=probe.reason,
                probe_feedback=validation_feedback,
            )
            logger.warning(
                "baseline deploy probe failed attempt %d/%d: %s",
                attempt,
                max_attempts,
                probe.reason,
            )
            spec_file = None
            continue

        return spec_file

    fail_iteration_phase(
        iteration_path=iteration_path,
        task_run_dir=ctx.task_run_dir,
        sample_dir=ctx.sample_dir,
        sample=ctx.sample,
        iteration_id=plan.iteration_id,
        failure_reason=last_err,
        kind="baseline" if is_baseline else "spec",
        logger=logger,
    )
    return None


def run_reuse_spec_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    *,
    source_iteration_id: str,
    logger: logging.Logger,
) -> SpecStageResult:
    """Reuse a prior spec.yaml (code refinement path)."""
    from tasks import esc

    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
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

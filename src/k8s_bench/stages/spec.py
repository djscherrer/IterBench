"""
Spec preparation stage.

Returns ``(spec_file, abort_sample)``. Three branches:

- **baseline**: call ``generate_baseline_spec_until_deployable`` (retries LLM +
  deploy probe until a working spec is produced). Failure aborts the sample
  (legacy ``break`` semantics).
- **reuse**: copy ``spec.yaml`` from the iteration named by ``plan.reuse_spec_from``
  (used for code-refinement iterations — code changes, spec stays).
- **generate**: LLM call + static validation + deploy probe.

Any non-baseline failure marks the iteration ``-spec-failed`` and returns
``(None, False)`` so the outer loop tries the next iteration.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..cluster.capacity import collect_cluster_capacity
from ..gates import probe_iteration_deployable
from ..iteration_failure import fail_iteration_phase
from ..spec.generation import (
    generate_and_write_spec,
    generate_baseline_spec_until_deployable,
    reuse_deployment_spec_for_iteration,
)
from ..workspace import (
    find_iteration_spec_path,
    resolve_iteration_dir,
    update_iteration_meta,
)
from ..orchestration.config import IterationPlan, RunConfig, SampleContext


def prepare_spec_or_fail(
    ctx: SampleContext,
    plan: IterationPlan,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
) -> tuple[Path | None, bool]:
    """Returns ``(spec_file, abort_sample)``. ``abort_sample`` only on baseline-fail."""
    from tasks import esc

    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    is_baseline = plan.refinement_action == "baseline"
    folder_kind = (
        "baseline"
        if is_baseline
        else ("code" if plan.refinement_action == "code" else "spec")
    )

    bench_labels_dict = {
        "baxbench.dev/model": esc(ctx.task.model),
        "baxbench.dev/scenario": esc(ctx.task.scenario.id),
        "baxbench.dev/env": esc(ctx.task.env.id),
        "baxbench.dev/spec-gen": "true",
        "baxbench.dev/phase": str(plan.iteration_index),
    }
    sample_slug = (
        f"{esc(ctx.task.model)}-{esc(ctx.task.env.id)}-"
        f"{esc(ctx.task.scenario.id)}-sample{ctx.sample}"
    )

    spec_file: Path | None = None

    if is_baseline:

        def _baseline_probe():
            return probe_iteration_deployable(
                iteration_path=iteration_path,
                image_id=image_id,
                sample_slug=sample_slug,
                app_port=ctx.task.env.port,
                needs_db=ctx.task.scenario.needs_db,
                wait_timeout_s=cfg.k8s_wait_timeout,
                labels=bench_labels_dict,
                logger=logger,
            )

        spec_file, baseline_err = generate_baseline_spec_until_deployable(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            iteration_id=plan.iteration_id,
            logger=logger,
            deploy_probe=_baseline_probe,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
            vllm_port=cfg.vllm_port,
            max_deploy_attempts=cfg.baseline_spec_max_attempts,
        )
        if spec_file is None:
            fail_iteration_phase(
                iteration_path=iteration_path,
                save_dir=ctx.save_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=plan.iteration_id,
                failure_reason=baseline_err
                or "baseline spec never became deployable",
                kind="baseline",
                logger=logger,
            )
            return None, True
        update_iteration_meta(iteration_path, spec_regenerated=True)

    elif plan.reuse_spec_from is not None:
        logger.info(
            "iteration %s: reusing deployment spec from %s (code refinement; "
            "no LLM spec generation)",
            plan.iteration_id,
            plan.reuse_spec_from,
        )
        spec_file = reuse_deployment_spec_for_iteration(
            iteration_path=iteration_path,
            sample_dir=ctx.sample_dir,
            source_iteration_id=plan.reuse_spec_from,
            target_iteration_id=plan.iteration_id,
            extra_labels=bench_labels_dict,
            logger=logger,
        )
        update_iteration_meta(
            iteration_path,
            spec_regenerated=False,
            spec_reused_from=plan.reuse_spec_from,
        )
    else:
        spec_file, gen_err = generate_and_write_spec(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            iteration_id=plan.iteration_id,
            logger=logger,
            capacity=collect_cluster_capacity(),
            prior_feedback=plan.prior.bench_feedback,
            max_validation_retries=1,
            iteration_index=plan.iteration_index,
            total_iterations=cfg.total_iterations,
            vllm_port=cfg.vllm_port,
        )
        if spec_file is None:
            fail_iteration_phase(
                iteration_path=iteration_path,
                save_dir=ctx.save_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=plan.iteration_id,
                failure_reason=gen_err or "static spec validation failed",
                kind="spec",
                logger=logger,
            )
            return None, False

        probe = probe_iteration_deployable(
            iteration_path=iteration_path,
            image_id=image_id,
            sample_slug=sample_slug,
            app_port=ctx.task.env.port,
            needs_db=ctx.task.scenario.needs_db,
            wait_timeout_s=cfg.k8s_wait_timeout,
            labels=bench_labels_dict,
            logger=logger,
        )
        if not probe.ok:
            fail_iteration_phase(
                iteration_path=iteration_path,
                save_dir=ctx.save_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=plan.iteration_id,
                failure_reason=probe.reason,
                kind="spec",
                logger=logger,
            )
            return None, False
        update_iteration_meta(iteration_path, spec_regenerated=True)

    spec_file = spec_file or find_iteration_spec_path(iteration_path)
    if spec_file is None:
        fail_iteration_phase(
            iteration_path=iteration_path,
            save_dir=ctx.save_dir,
            sample_dir=ctx.sample_dir,
            sample=ctx.sample,
            iteration_id=plan.iteration_id,
            failure_reason="no spec.yaml after prepare",
            kind=folder_kind,
            logger=logger,
        )
        return None, False

    return spec_file, False

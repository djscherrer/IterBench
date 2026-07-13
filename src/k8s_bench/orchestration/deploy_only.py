"""
Deploy-only execution: deploy + Locust bench for an existing iteration folder.

Mirrors the ``04-deploy → 05-bench → 06-outcome`` tail of
:func:`k8s_bench.orchestration.execute.execute_iteration` — same stage engines,
same per-stage loggers — but skips the ``01-decision``/``02-code``/``03-spec``
LLM stages. Application code and ``03-spec/spec.yaml`` must already exist on disk
(hand-edited or copied). The deploy stage patches the registry image, port, and
labels onto the spec in memory and writes ``04-deploy/probe.json``; bench reads
that probe. Used by ``--deploy-only`` runs and ``scripts/k8s_run_iteration.sh``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..code.docker_image import ensure_docker_image
from ..experiment_summary import append_perf_run_block
from ..failure import fail_iteration_phase
from ..failure.persist import (
    build_bench_iteration_failure,
    build_deploy_iteration_failure,
)
from ..feedback import collect_iteration_feedback
from ..stages.bench import run_bench_attempt
from ..stages.deploy import run_deploy_attempt
from ..workspace import (
    bench_dir_has_complete_run,
    clear_bench_dir_if_present,
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    iteration_bench_dir,
    iteration_bench_log_path,
    iteration_deploy_log_path,
    iteration_log_path,
    nonempty_code_snapshot_dir,
    parse_iteration_index,
    prior_iteration_code_dir,
    update_iteration_meta,
    write_feedback,
)
from ..workspace.skips import append_k8s_skip
from .config import IterationPlan, RunConfig, SampleContext
from .lineage import IterationLineage


def _sample_slug(ctx: SampleContext) -> str:
    from tasks import esc

    return (
        f"{esc(ctx.task.model)}-{esc(ctx.task.env.id)}-"
        f"{esc(ctx.task.scenario.id)}-sample{ctx.sample}"
    )


def _bench_labels(ctx: SampleContext, plan: IterationPlan) -> dict[str, str]:
    from tasks import esc

    return {
        "baxbench.dev/model": esc(ctx.task.model),
        "baxbench.dev/scenario": esc(ctx.task.scenario.id),
        "baxbench.dev/env": esc(ctx.task.env.id),
        "baxbench.dev/spec-gen": "true",
        "baxbench.dev/phase": str(plan.iteration_index),
    }


def _deploy_only_plan(iteration_path: Path) -> IterationPlan:
    iteration_id = iteration_path.name
    idx = parse_iteration_index(iteration_id) or 0
    lineage = IterationLineage(
        bench_feedback=None,
        prior_iteration_failure=None,
        prior_code_dir=None,
        latest_spec=None,
    )
    return IterationPlan(
        iteration_id=iteration_id,
        iteration_index=idx,
        refinement_action="code",
        decision=None,
        lineage=lineage,
    )


def _write_iteration_header(
    iteration_path: Path, plan: IterationPlan, cfg: RunConfig
) -> None:
    path = iteration_log_path(iteration_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    path.write_text(
        f"{ts} k8s deploy-only experiment={cfg.experiment_id} "
        f"iteration={plan.iteration_id} load_profile={cfg.load_profile}\n",
        encoding="utf-8",
    )


def _append_iteration_outcome(iteration_path: Path, outcome: str) -> None:
    path = iteration_log_path(iteration_path)
    if not path.is_file():
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} outcome={outcome}\n")


def _resolve_source_code_dir(
    ctx: SampleContext, iteration_path: Path
) -> Path | None:
    code_snap = nonempty_code_snapshot_dir(iteration_path)
    if code_snap is not None:
        return code_snap
    idx = parse_iteration_index(iteration_path.name)
    if idx is not None and idx > 0:
        return prior_iteration_code_dir(
            ctx.sample_dir, idx, experiment_id=ctx.experiment_id
        )
    return None


def _record_outcome(
    ctx: SampleContext,
    plan: IterationPlan,
    iteration_path: Path,
    run_dir: Path,
    cfg: RunConfig,
    logger: logging.Logger,
) -> None:
    fb = collect_iteration_feedback(
        perf_run_dir=run_dir,
        iteration_path=iteration_path,
        logger=logger,
    )
    write_feedback(run_dir, fb)
    update_iteration_meta(
        iteration_path,
        status="success",
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        summary_path = append_perf_run_block(
            sample_dir=ctx.sample_dir,
            iteration_id=plan.iteration_id,
            perf_run_dir=run_dir,
            feedback=fb,
            load_profile=cfg.load_profile,
        )
        logger.info("Updated experiment summary: %s", summary_path)
    except Exception as exc:
        logger.warning("Could not update experiment summary: %s", exc)


def execute_deploy_only_iteration(
    ctx: SampleContext,
    iteration_path: Path,
    cfg: RunConfig,
) -> Path | None:
    """Deploy, bench, and record outcome for one existing iteration folder."""
    task = ctx.task
    iteration_id = iteration_path.name
    plan = _deploy_only_plan(iteration_path)
    bench_dir = iteration_bench_dir(iteration_path)

    if not cfg.force and bench_dir_has_complete_run(bench_dir):
        append_k8s_skip(
            ctx.task_run_dir,
            ctx.sample,
            f"skipped: k8s perf run already exists for "
            f"iteration={iteration_id!r} load_profile={cfg.load_profile!r}",
        )
        return None

    if find_iteration_spec_path(iteration_path) is None:
        append_k8s_skip(
            ctx.task_run_dir,
            ctx.sample,
            f"skipped: missing spec.yaml for {iteration_id}",
        )
        return None

    source_code_dir = _resolve_source_code_dir(ctx, iteration_path)
    if source_code_dir is None:
        append_k8s_skip(
            ctx.task_run_dir,
            ctx.sample,
            f"skipped: no code snapshot on {iteration_id} "
            "and none on the previous iteration",
        )
        return None

    ensure_iteration_core_layout(iteration_path)
    if cfg.force:
        clear_bench_dir_if_present(iteration_path)
    run_dir = bench_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_iteration_header(iteration_path, plan, cfg)

    with task.create_logger(iteration_deploy_log_path(iteration_path)) as deploy_logger:
        image_id = ensure_docker_image(
            task,
            ctx.results_dir,
            ctx.sample,
            ctx.base_image_id,
            deploy_logger,
            code_dir=source_code_dir,
        )
        if image_id is None:
            append_k8s_skip(
                ctx.task_run_dir,
                ctx.sample,
                f"skipped: failed to build docker image for {iteration_id}",
            )
            _append_iteration_outcome(iteration_path, "image-build-failed")
            return None

        deploy = run_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            image_id=image_id,
            sample_slug=_sample_slug(ctx),
            app_port=task.env.port,
            needs_db=task.scenario.needs_db,
            k8s_cluster=ctx.k8s_cluster,
            wait_timeout_s=cfg.k8s_wait_timeout,
            labels=_bench_labels(ctx, plan),
            logger=deploy_logger,
            attempt_index=1,
            enable_attempts=False,
        )
        if not deploy.ok:
            iteration_failure = build_deploy_iteration_failure(
                iteration_path,
                iteration_id=iteration_id,
                terminal_attempt=1,
                fallback=deploy.failure,
                logger=deploy_logger,
            )
            fail_iteration_phase(
                iteration_path=iteration_path,
                task_run_dir=ctx.task_run_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=iteration_id,
                kind="deploy",
                logger=deploy_logger,
                iteration_failure=iteration_failure,
            )
            _append_iteration_outcome(iteration_path, "deploy-failed")
            return None

    with task.create_logger(iteration_bench_log_path(iteration_path)) as bench_logger:
        bench = run_bench_attempt(
            task=task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            run_dir=run_dir,
            bench_users=cfg.bench_users,
            bench_spawn_rate=cfg.bench_spawn_rate,
            bench_run_time=cfg.bench_run_time,
            iteration_index=plan.iteration_index,
            iteration_id=iteration_id,
            logger=bench_logger,
            load_profile=cfg.load_profile,
            k8s_cluster=ctx.k8s_cluster,
            attempt_index=1,
            enable_attempts=False,
        )
        if not bench.ok:
            iteration_failure = build_bench_iteration_failure(
                iteration_path,
                iteration_id=iteration_id,
                terminal_attempt=1,
                fallback=bench.failure,
                logger=bench_logger,
            )
            fail_iteration_phase(
                iteration_path=iteration_path,
                task_run_dir=ctx.task_run_dir,
                sample_dir=ctx.sample_dir,
                sample=ctx.sample,
                iteration_id=iteration_id,
                kind="bench",
                logger=bench_logger,
                iteration_failure=iteration_failure,
            )
            _append_iteration_outcome(iteration_path, "bench-failed")
            return None

        try:
            _record_outcome(ctx, plan, iteration_path, run_dir, cfg, bench_logger)
        except Exception as exc:
            bench_logger.warning("Could not write iteration feedback: %s", exc)

        bench_logger.info(
            "finished deploy-only bench sample=%d iteration=%s",
            ctx.sample,
            iteration_id,
        )

    _append_iteration_outcome(iteration_path, "ok")
    return run_dir


__all__ = ["execute_deploy_only_iteration"]

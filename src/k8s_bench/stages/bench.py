"""
Locust bench stage.

Deploys the iteration manifests, runs Locust against the cluster, and writes
the bench run artifacts (CSVs, kubectl-top samples, bench.log).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..iteration import run_k8s_bench_iteration
from ..util.sample import (
    append_k8s_skip,
    bench_labels,
    ensure_docker_image,
    performance_test_names,
    resolve_locustfile,
)
from ..workspace import resolve_iteration_dir
from ..orchestration.config import IterationPlan, RunConfig, SampleContext


def run_bench(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
) -> None:
    """Run the Locust bench for one iteration (no exception escapes)."""
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    run_locust_for_iteration(
        ctx.task,
        ctx.results_dir,
        ctx.sample,
        iteration_path,
        run_dir,
        image_id,
        timeout=cfg.timeout,
        bench_users=cfg.bench_users,
        bench_spawn_rate=cfg.bench_spawn_rate,
        bench_run_time=cfg.bench_run_time,
        k8s_wait_timeout=cfg.k8s_wait_timeout,
        iteration_index=plan.iteration_index,
        logger=logger,
        rebuild_code_dir=plan.source_code_dir,
    )


def run_locust_for_iteration(
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    run_dir: Path,
    image_id: str,
    *,
    timeout: int,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    k8s_wait_timeout: int,
    iteration_index: int | None = None,
    logger: logging.Logger,
    rebuild_code_dir: Path | None = None,
) -> bool:
    """
    Deploy + Locust for a single iteration directory.

    Exposed as a public helper because the deploy-only path
    (``run_deploy_only_k8s_bench``) calls it directly without going through the
    iterative loop.
    """
    from tasks import esc

    save_dir = task.get_save_dir(results_dir)
    iteration_id = iteration_path.name
    tests = performance_test_names(task)
    if not tests:
        append_k8s_skip(
            save_dir,
            sample,
            f"skipped iteration {iteration_id}: no performance tests"
            if iteration_index
            else "skipped: no performance tests configured",
        )
        return False

    image_id = ensure_docker_image(
        task, results_dir, sample, image_id, logger, code_dir=rebuild_code_dir
    )
    if image_id is None:
        append_k8s_skip(
            save_dir,
            sample,
            f"skipped iteration {iteration_id}: failed to build docker image"
            if iteration_index
            else "skipped: failed to build docker image for k8s bench",
        )
        return False

    locustfile = resolve_locustfile(task, run_dir)
    if locustfile is None:
        append_k8s_skip(save_dir, sample, "skipped: missing locustfile")
        return False

    sample_slug = (
        f"{esc(task.model)}-{esc(task.env.id)}-"
        f"{esc(task.scenario.id)}-sample{sample}"
    )
    labels = bench_labels(task, iteration_index=iteration_index)

    for test in tests:
        csv_prefix = run_dir / f"bench_results_{test}"
        logger.info(
            "running k8s bench iteration=%s locustfile=%s",
            iteration_id,
            locustfile,
        )
        try:
            run_k8s_bench_iteration(
                iteration_path=iteration_path,
                run_dir=run_dir,
                image_id=image_id,
                sample_slug=sample_slug,
                app_port=task.env.port,
                needs_db=task.scenario.needs_db,
                locustfile=locustfile,
                csv_prefix=csv_prefix,
                timeout=timeout,
                locust_user=test,
                bench_users=bench_users,
                bench_spawn_rate=bench_spawn_rate,
                bench_run_time=bench_run_time,
                wait_timeout_s=k8s_wait_timeout,
                labels=labels,
                logger=logger,
            )
        except Exception as e:
            logger.exception("k8s bench failed: %s", e, exc_info=e)

    return True

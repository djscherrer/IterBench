"""
Locust bench stage.

Deploys the iteration manifests, runs Locust against the cluster, and writes
the bench run artifacts (CSVs, kubectl-top samples, bench.log).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from locust_bench.paths import locust_csv_prefix

from ..code.docker_image import ensure_docker_image
from ..iteration import run_k8s_bench_iteration
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import (
    experiment_root_from_iteration_path,
    iteration_code_snapshot_dir,
    resolve_iteration_dir,
)
from ..workspace.skips import append_k8s_skip


def run_bench_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
) -> None:
    """Run the Locust bench for one iteration (no exception escapes)."""
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    rebuild_code_dir = plan.lineage.latest_code_dir or iteration_code_snapshot_dir(
        iteration_path
    )
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
        rebuild_code_dir=rebuild_code_dir,
        load_profile=cfg.load_profile,
        k8s_cluster=ctx.k8s_cluster,
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
    load_profile: str = "default",
    k8s_cluster: str = "",
) -> bool:
    """
    Deploy + Locust for a single iteration directory.

    Exposed as a public helper because the deploy-only path
    Deploy-only paths (``--deploy-only`` on :func:`k8s_bench.loop.run_k8s_bench`)
    call it directly without going through the iterative orchestrator.
    """
    from tasks import esc

    task_run_dir = task.get_save_dir(results_dir)
    iteration_id = iteration_path.name
    tests = _performance_test_names(task)
    if not tests:
        append_k8s_skip(
            task_run_dir,
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
            task_run_dir,
            sample,
            f"skipped iteration {iteration_id}: failed to build docker image"
            if iteration_index
            else "skipped: failed to build docker image for k8s bench",
        )
        return False

    locustfile = _resolve_locustfile(task, run_dir)
    if locustfile is None:
        append_k8s_skip(task_run_dir, sample, "skipped: missing locustfile")
        return False

    sample_slug = (
        f"{esc(task.model)}-{esc(task.env.id)}-"
        f"{esc(task.scenario.id)}-sample{sample}"
    )
    labels = _bench_labels(task, iteration_index=iteration_index)

    for test in tests:
        csv_prefix = locust_csv_prefix(run_dir, test)
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
                load_profile=load_profile,
                k8s_cluster=k8s_cluster,
                logger=logger,
            )
        except Exception as e:
            logger.exception("k8s bench failed: %s", e, exc_info=e)
            return False

    from ..plots import refresh_plots_after_bench
    from ..workspace import experiment_root_from_iteration_path

    refresh_plots_after_bench(
        run_dir,
        experiment_root_from_iteration_path(iteration_path),
        logger=logger,
    )
    return True


def _performance_test_names(task: Any) -> list[str]:
    if task.scenario.performance_tests:
        return list(task.scenario.performance_tests)
    from scenario_files import SCENARIO_FILE_PATH

    shared = SCENARIO_FILE_PATH.joinpath(f"locustfiles/{task.scenario.id.lower()}.py")
    if shared.is_file() or task.scenario.locustfile:
        return ["default"]
    return []


def _resolve_locustfile(task: Any, run_dir: Path) -> Path | None:
    from locust_bench.paths import locust_dir
    from scenario_files import SCENARIO_FILE_PATH

    shared = SCENARIO_FILE_PATH.joinpath(f"locustfiles/{task.scenario.id.lower()}.py")
    if task.scenario.locustfile:
        locustfile = locust_dir(run_dir) / f"locustfile-{task.scenario.id.lower()}.py"
        locustfile.write_text(task.scenario.locustfile, encoding="utf-8")
        return locustfile
    if shared.is_file():
        return shared
    return None


def _bench_labels(task: Any, *, iteration_index: int | None = None) -> dict[str, str]:
    from tasks import esc

    labels = {
        "baxbench.dev/model": esc(task.model),
        "baxbench.dev/scenario": esc(task.scenario.id),
        "baxbench.dev/env": esc(task.env.id),
    }
    if iteration_index is not None:
        # Kept as ``baxbench.dev/phase`` for back-compat with existing kubectl
        # filters and dashboards; semantically this is the iteration index.
        labels["baxbench.dev/phase"] = str(iteration_index)
    return labels

"""
Locust bench stage (``05-bench/``).

Orchestrates the bench phase: deploy the iteration image, run Locust load
tests, refresh plots. Single-attempt logic and stage-level wiring live here.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locust_bench.paths import locust_csv_prefix

from ..code.docker_image import ensure_docker_image
from ..failure import BenchFailureRecord, fail_iteration_phase
from ..failure.persist import build_bench_iteration_failure
from ..feedback import read_failed_iteration_error_excerpt
from ..iteration import run_k8s_bench_iteration
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import (
    iteration_bench_dir,
    resolve_bench_rebuild_code_dir,
    resolve_iteration_dir,
)
from ..workspace.skips import append_k8s_skip


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BenchAttemptResult:
    """Outcome of one bench run attempt."""

    ok: bool = False
    error: str = ""
    failure: BenchFailureRecord | None = None


@dataclass(frozen=True)
class BenchStageResult:
    """Success when ``attempt`` is set (bench run completed)."""

    attempt: BenchAttemptResult | None = None


# ---------------------------------------------------------------------------
# Attempt-level helpers
# ---------------------------------------------------------------------------

def rotate_top_level_into_attempt(
    iteration_path: Path,
    attempt_dir: Path,
) -> None:
    """Move top-level bench artifacts into ``attempts/<NNN>/`` before retry."""
    bench_dir = iteration_bench_dir(iteration_path)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name in ("bench.log",):
        src = bench_dir / name
        if src.is_file():
            shutil.move(str(src), str(attempt_dir / name))


def build_bench_failure_record(
    *,
    iteration_path: Path,
    iteration_id: str,
    error: str,
    attempt: int | None = None,
) -> BenchFailureRecord:
    excerpt = read_failed_iteration_error_excerpt(iteration_path)
    if not excerpt:
        excerpt = error
    return BenchFailureRecord(
        phase="bench",
        kind="bench_run",
        iteration_id=iteration_id,
        attempt=attempt,
        summary=error or "benchmark run failed",
        diagnostic_excerpt=excerpt,
    )


def run_bench_attempt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    run_dir: Path,
    image_id: str,
    timeout: int,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    k8s_wait_timeout: int,
    iteration_index: int | None,
    iteration_id: str,
    logger: logging.Logger,
    rebuild_code_dir: Path | None = None,
    load_profile: str = "default",
    k8s_cluster: str = "",
    attempt_index: int = 1,
    enable_attempts: bool = False,
) -> BenchAttemptResult:
    ok, error = _run_locust(
        task=task,
        results_dir=results_dir,
        sample=sample,
        iteration_path=iteration_path,
        run_dir=run_dir,
        image_id=image_id,
        timeout=timeout,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
        k8s_wait_timeout=k8s_wait_timeout,
        iteration_index=iteration_index,
        logger=logger,
        rebuild_code_dir=rebuild_code_dir,
        load_profile=load_profile,
        k8s_cluster=k8s_cluster,
    )
    if ok:
        return BenchAttemptResult(ok=True)

    failure = build_bench_failure_record(
        iteration_path=iteration_path,
        iteration_id=iteration_id,
        error=error,
        attempt=attempt_index,
    )
    from ..failure.persist import persist_bench_attempt_failure

    persist_bench_attempt_failure(
        iteration_path=iteration_path,
        attempt_index=attempt_index,
        enable_attempts=enable_attempts,
        record=failure,
        logger=logger,
    )
    return BenchAttemptResult(ok=False, error=error, failure=failure)


def _run_locust(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    run_dir: Path,
    image_id: str,
    timeout: int,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    k8s_wait_timeout: int,
    iteration_index: int | None,
    logger: logging.Logger,
    rebuild_code_dir: Path | None,
    load_profile: str,
    k8s_cluster: str,
) -> tuple[bool, str]:
    from tasks import esc

    task_run_dir = task.get_save_dir(results_dir)
    folder_id = iteration_path.name
    tests = _performance_test_names(task)
    if not tests:
        append_k8s_skip(
            task_run_dir,
            sample,
            f"skipped iteration {folder_id}: no performance tests"
            if iteration_index
            else "skipped: no performance tests configured",
        )
        return False, "no performance tests configured"

    image_id = ensure_docker_image(
        task, results_dir, sample, image_id, logger, code_dir=rebuild_code_dir
    )
    if image_id is None:
        append_k8s_skip(
            task_run_dir,
            sample,
            f"skipped iteration {folder_id}: failed to build docker image"
            if iteration_index
            else "skipped: failed to build docker image for k8s bench",
        )
        return False, "failed to build docker image for k8s bench"

    locustfile = _resolve_locustfile(task, run_dir)
    if locustfile is None:
        append_k8s_skip(task_run_dir, sample, "skipped: missing locustfile")
        return False, "missing locustfile"

    sample_slug = (
        f"{esc(task.model)}-{esc(task.env.id)}-"
        f"{esc(task.scenario.id)}-sample{sample}"
    )
    labels = _bench_labels(task, iteration_index=iteration_index)

    for test in tests:
        csv_prefix = locust_csv_prefix(run_dir, test)
        logger.info(
            "running k8s bench iteration=%s locustfile=%s",
            folder_id,
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
            return False, str(e) or e.__class__.__name__

    from ..plots import refresh_plots_after_bench
    from ..workspace import experiment_root_from_iteration_path

    refresh_plots_after_bench(
        run_dir,
        experiment_root_from_iteration_path(iteration_path),
        logger=logger,
    )
    return True, ""


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
        labels["baxbench.dev/phase"] = str(iteration_index)
    return labels


# ---------------------------------------------------------------------------
# Stage-level orchestration
# ---------------------------------------------------------------------------

def run_bench_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
    *,
    rebuild_code_dir: Path | None = None,
) -> BenchStageResult:
    """Run the Locust bench for one iteration."""
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    if rebuild_code_dir is None:
        rebuild_code_dir = resolve_bench_rebuild_code_dir(
            ctx.sample_dir,
            iteration_path,
            experiment_id=ctx.experiment_id,
        )
    result = run_bench_attempt(
        task=ctx.task,
        results_dir=ctx.results_dir,
        sample=ctx.sample,
        iteration_path=iteration_path,
        run_dir=run_dir,
        image_id=image_id,
        timeout=cfg.timeout,
        bench_users=cfg.bench_users,
        bench_spawn_rate=cfg.bench_spawn_rate,
        bench_run_time=cfg.bench_run_time,
        k8s_wait_timeout=cfg.k8s_wait_timeout,
        iteration_index=plan.iteration_index,
        iteration_id=plan.iteration_id,
        logger=logger,
        rebuild_code_dir=rebuild_code_dir,
        load_profile=cfg.load_profile,
        k8s_cluster=ctx.k8s_cluster,
        attempt_index=1,
        enable_attempts=False,
    )
    if result.ok:
        return BenchStageResult(attempt=result)

    iteration_failure = build_bench_iteration_failure(
        iteration_path,
        iteration_id=plan.iteration_id,
        terminal_attempt=1,
        fallback=result.failure,
        logger=logger,
    )
    fail_iteration_phase(
        iteration_path=iteration_path,
        task_run_dir=ctx.task_run_dir,
        sample_dir=ctx.sample_dir,
        sample=ctx.sample,
        iteration_id=plan.iteration_id,
        kind="bench",
        logger=logger,
        iteration_failure=iteration_failure,
    )
    return BenchStageResult()


__all__ = [
    "BenchAttemptResult",
    "BenchStageResult",
    "build_bench_failure_record",
    "rotate_top_level_into_attempt",
    "run_bench_attempt",
    "run_bench_stage",
]

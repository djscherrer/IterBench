"""
Locust bench stage (``05-bench/``).

Runs Locust load tests against an iteration that already passed ``04-deploy``.
Does not render manifests, ``kubectl apply``, build images, or push to the registry.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locust_bench.paths import locust_csv_prefix

from ..failure import BenchFailureRecord, fail_iteration_phase
from ..failure.bench_diagnostics import collect_bench_failure_diagnostics
from ..failure.classify import classify_bench_failure_kind
from ..failure.persist import build_bench_iteration_failure
from ..feedback import read_failed_iteration_error_excerpt
from ..iteration import run_k8s_bench_iteration
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import (
    iteration_bench_dir,
    iteration_bench_log_path,
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
    """Set ``ok=True`` when the Locust run completed."""

    ok: bool = False


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
    for name in (iteration_bench_log_path(iteration_path).name,):
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
    diagnostic_excerpt = collect_bench_failure_diagnostics(iteration_path)
    classify_text = "\n".join(
        part for part in (error, diagnostic_excerpt) if part and part.strip()
    )
    if not classify_text.strip():
        classify_text = read_failed_iteration_error_excerpt(iteration_path) or error
    kind = classify_bench_failure_kind(classify_text)
    if not diagnostic_excerpt:
        diagnostic_excerpt = classify_text
    return BenchFailureRecord(
        phase="bench",
        kind=kind,  # type: ignore[arg-type]
        iteration_id=iteration_id,
        attempt=attempt,
        summary=error or "benchmark run failed",
        diagnostic_excerpt=diagnostic_excerpt,
    )


def run_bench_attempt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    run_dir: Path,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    iteration_index: int | None,
    iteration_id: str,
    logger: logging.Logger,
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
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
        iteration_index=iteration_index,
        logger=logger,
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
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    iteration_index: int | None,
    logger: logging.Logger,
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

    locustfile = _resolve_locustfile(task, run_dir)
    if locustfile is None:
        append_k8s_skip(task_run_dir, sample, "skipped: missing locustfile")
        return False, "missing locustfile"

    sample_slug = (
        f"{esc(task.model)}-{esc(task.env.id)}-"
        f"{esc(task.scenario.id)}-sample{sample}"
    )

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
                sample_slug=sample_slug,
                locustfile=locustfile,
                csv_prefix=csv_prefix,
                bench_users=bench_users,
                bench_spawn_rate=bench_spawn_rate,
                bench_run_time=bench_run_time,
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


# ---------------------------------------------------------------------------
# Stage-level orchestration
# ---------------------------------------------------------------------------

def run_bench_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    cfg: RunConfig,
    logger: logging.Logger,
) -> BenchStageResult:
    """Run the Locust bench for one iteration (deploy must have succeeded)."""
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    result = run_bench_attempt(
        task=ctx.task,
        results_dir=ctx.results_dir,
        sample=ctx.sample,
        iteration_path=iteration_path,
        run_dir=run_dir,
        bench_users=cfg.bench_users,
        bench_spawn_rate=cfg.bench_spawn_rate,
        bench_run_time=cfg.bench_run_time,
        iteration_index=plan.iteration_index,
        iteration_id=plan.iteration_id,
        logger=logger,
        load_profile=cfg.load_profile,
        k8s_cluster=ctx.k8s_cluster,
        attempt_index=1,
        enable_attempts=False,
    )
    if result.ok:
        return BenchStageResult(ok=True)

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

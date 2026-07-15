"""
Sample-level setup and teardown.

``sample_preflight`` runs once per sample BEFORE any iteration: it ensures the
k8s experiment workspace exists and returns a :class:`SampleContext` with no
``base_image_id`` yet (set after iteration-000 baseline codegen in
``execute_iteration``).

``sample_postlude`` runs once AFTER all iterations of the sample: refresh LLM
cost summary, clean up k8s namespaces.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..cluster.cleanup import cleanup_baxbench_namespaces_after_bench
from ..llm_cost import refresh_k8s_cost_summary
from ..stages.decision import resolve_refinement_mode
from ..code.docker_image import ensure_docker_image
from ..code.shared import functional_tests_passed_at
from ..workspace.skips import append_k8s_skip
from ..workspace import (
    image_id_from_test_log,
    iteration_code_snapshot_dir,
    iteration_functional_tests_dir,
    iteration_id_for_index,
    iterations_root,
    k8s_workspace_root,
    materialize_code_lineage,
    normalize_experiment_id,
    nonempty_code_snapshot_dir,
    parse_iteration_index,
    prior_iteration_code_dir,
    resolve_iteration_dir,
)
from .config import RunConfig, SampleContext


def build_run_config(
    *,
    timeout: int,
    force: bool,
    k8s_cluster: str,
    k8s_iteration: str | None,
    k8s_iterations: int,
    k8s_wait_timeout: int,
    k8s_refinement: str,
    ft_timeout: int | None,
    num_ports: int,
    min_port: int,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    load_profile: str = "quick-check",
    k8s_experiment_id: str | None = None,
    llm_max_cost_usd: float | None = None,
    baseline_code_max_attempts: int = 3,
    baseline_spec_max_attempts: int = 5,
) -> RunConfig:
    """Build :class:`RunConfig` from explicit caller parameters."""
    cluster = (k8s_cluster or "").strip()
    if not cluster:
        raise ValueError("k8s_cluster is required")
    experiment_id = normalize_experiment_id(k8s_experiment_id or "default")
    iteration_ids = _plan_iteration_ids(
        num_refinement_iterations=k8s_iterations,
        explicit_iteration=k8s_iteration,
    )
    return RunConfig(
        load_profile=load_profile,
        experiment_id=experiment_id,
        k8s_cluster=cluster,
        llm_max_cost_usd=llm_max_cost_usd,
        refinement_mode=resolve_refinement_mode(k8s_refinement),
        iteration_ids=iteration_ids,
        total_iterations=len(iteration_ids),
        timeout=timeout,
        ft_timeout=ft_timeout if ft_timeout is not None else timeout,
        k8s_wait_timeout=k8s_wait_timeout,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
        num_ports=num_ports,
        min_port=min_port,
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        force=force,
        baseline_code_max_attempts=baseline_code_max_attempts,
        baseline_spec_max_attempts=baseline_spec_max_attempts,
    )


def _plan_iteration_ids(
    *,
    num_refinement_iterations: int,
    explicit_iteration: str | None = None,
) -> list[str]:
    """
    Return iteration ids for one experiment.

    ``num_refinement_iterations=N`` yields ``iteration-000`` (baseline) plus
    ``iteration-001`` … ``iteration-{N:03d}`` (N refinement iterations).
    """
    from ..workspace import normalize_iteration_id

    if explicit_iteration:
        return [normalize_iteration_id(explicit_iteration)]
    if num_refinement_iterations < 0:
        raise ValueError("num_refinement_iterations must be >= 0")
    return [iteration_id_for_index(i) for i in range(0, num_refinement_iterations + 1)]


def sample_preflight(
    task: Any,
    results_dir: Path,
    sample: int,
    cfg: RunConfig,
) -> SampleContext | None:
    """Ensure experiment workspace exists; return context (no baseline codegen)."""
    task_run_dir = task.get_save_dir(results_dir)
    sample_dir = task.get_sample_dir(results_dir, sample)
    k8s_workspace_root(sample_dir, experiment_id=cfg.experiment_id).mkdir(
        parents=True, exist_ok=True
    )
    iterations_root(sample_dir, experiment_id=cfg.experiment_id).mkdir(
        parents=True, exist_ok=True
    )
    # Build the conversational LLM session once per (sample, experiment) and
    # thread it through all iterative stages.
    from ..session import get_experiment_session

    session = get_experiment_session(
        task,
        sample_dir,
        sample,
        experiment_id=cfg.experiment_id,
        logger=logging.getLogger(task.id),
    )
    return SampleContext(
        task=task,
        results_dir=results_dir,
        sample=sample,
        sample_dir=sample_dir,
        task_run_dir=task_run_dir,
        experiment_id=cfg.experiment_id,
        k8s_cluster=cfg.k8s_cluster,
        llm_max_cost_usd=cfg.llm_max_cost_usd,
        session=session,
    )


def sample_context_from_baseline_disk(
    task: Any,
    results_dir: Path,
    sample: int,
    cfg: RunConfig,
) -> SampleContext | None:
    """
    Build :class:`SampleContext` from an existing baseline iteration on disk.

    Used by deploy-only runs that bench prior iterations without regenerating
    baseline code.
    """
    task_run_dir = task.get_save_dir(results_dir)
    sample_dir = task.get_sample_dir(results_dir, sample)
    iteration_path = resolve_iteration_dir(
        sample_dir, iteration_id_for_index(0), experiment_id=cfg.experiment_id
    )
    sample_logger = logging.getLogger(task.id)
    ctx = _deploy_only_iteration_preflight(
        task,
        results_dir,
        sample,
        sample_dir,
        task_run_dir,
        iteration_path,
        sample_logger,
        experiment_id=cfg.experiment_id,
        k8s_cluster=cfg.k8s_cluster,
        llm_max_cost_usd=cfg.llm_max_cost_usd,
    )
    if ctx is not None:
        return ctx
    append_k8s_skip(
        task_run_dir,
        sample,
        "skipped deploy-only: no passing baseline functional tests on disk",
    )
    return None


def deploy_only_preflight(
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    cfg: RunConfig,
) -> SampleContext | None:
    """
    Preflight for ``--k8s-iteration-path`` deploy-only runs.

    Prefer iteration-local functional tests under ``02-code/``; materialize code
    from the previous iteration snapshot if needed.
    """
    task_run_dir = task.get_save_dir(results_dir)
    sample_dir = task.get_sample_dir(results_dir, sample)
    sample_logger = logging.getLogger(task.id)
    experiment_id = cfg.experiment_id

    ctx = _deploy_only_iteration_preflight(
        task,
        results_dir,
        sample,
        sample_dir,
        task_run_dir,
        iteration_path,
        sample_logger,
        experiment_id=experiment_id,
        k8s_cluster=cfg.k8s_cluster,
        llm_max_cost_usd=cfg.llm_max_cost_usd,
    )
    if ctx is not None:
        return ctx

    idx = parse_iteration_index(iteration_path.name)
    source_code = (
        prior_iteration_code_dir(
            sample_dir, idx, experiment_id=cfg.experiment_id
        )
        if idx is not None and idx > 0
        else None
    )
    if source_code is not None:
        materialize_code_lineage(iteration_path, source_code)

    ctx = _deploy_only_iteration_preflight(
        task,
        results_dir,
        sample,
        sample_dir,
        task_run_dir,
        iteration_path,
        sample_logger,
        experiment_id=experiment_id,
        k8s_cluster=cfg.k8s_cluster,
        llm_max_cost_usd=cfg.llm_max_cost_usd,
    )
    if ctx is not None:
        return ctx

    append_k8s_skip(
        task_run_dir,
        sample,
        f"skipped deploy-only {iteration_path.name}: no passing functional "
        "tests on iteration or materialized code",
    )
    return None


def _deploy_only_iteration_preflight(
    task: Any,
    results_dir: Path,
    sample: int,
    sample_dir: Path,
    task_run_dir: Path,
    iteration_path: Path,
    sample_logger: logging.Logger,
    *,
    experiment_id: str = "default",
    k8s_cluster: str = "",
    llm_max_cost_usd: float | None = None,
) -> SampleContext | None:
    iter_ft_results = iteration_functional_tests_dir(iteration_path) / "test_results.json"
    iter_test_log = iteration_functional_tests_dir(iteration_path) / "test.log"
    code_snap = iteration_code_snapshot_dir(iteration_path)

    if not functional_tests_passed_at(iter_ft_results):
        return None

    image_id = (
        image_id_from_test_log(iter_test_log)
        if iter_test_log.is_file()
        else None
    )
    image_id = ensure_docker_image(
        task,
        results_dir,
        sample,
        image_id,
        sample_logger,
        code_dir=code_snap if code_snap.is_dir() and any(code_snap.iterdir()) else None,
    )
    if image_id is None:
        append_k8s_skip(
            task_run_dir,
            sample,
            f"skipped: failed to build docker image for {iteration_path.name}",
        )
        return None
    return SampleContext(
        task=task,
        results_dir=results_dir,
        sample=sample,
        sample_dir=sample_dir,
        task_run_dir=task_run_dir,
        base_image_id=image_id,
        experiment_id=experiment_id,
        k8s_cluster=k8s_cluster,
        llm_max_cost_usd=llm_max_cost_usd,
    )


def sample_postlude(
    ctx: SampleContext, *, cleanup_namespaces: bool = True
) -> None:
    """Refresh LLM cost summary; optionally clean up baxbench namespaces."""
    sample_logger = logging.getLogger(ctx.task.id)
    try:
        refresh_k8s_cost_summary(ctx.sample_dir, experiment_id=ctx.experiment_id)
    except Exception as exc:
        sample_logger.warning("Could not refresh LLM cost summary: %s", exc)

    if not cleanup_namespaces:
        sample_logger.info(
            "Skipping post-sample namespace cleanup (no new bench runs this sample)"
        )
        return

    try:
        cleanup_baxbench_namespaces_after_bench(logger=sample_logger)
    except Exception as exc:
        sample_logger.warning("Post-bench namespace cleanup failed: %s", exc)

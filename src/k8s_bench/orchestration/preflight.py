"""
Sample-level setup and teardown.

``sample_preflight`` runs once per sample BEFORE any iteration: it gates on
functional tests, resolves the baseline docker image id from the FT log, and
ensures the image is loadable. ``sample_postlude`` runs once AFTER all
iterations of the sample: refresh LLM cost summary, clean up k8s namespaces.

Reused by both ``run_iterative_k8s_bench`` and ``run_deploy_only_k8s_bench``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..cluster.cleanup import cleanup_baxbench_namespaces_after_bench
from ..llm_cost import refresh_k8s_cost_summary
from ..refinement.decision import resolve_refinement_mode
from ..util.sample import (
    append_k8s_skip,
    ensure_docker_image,
    functional_tests_gate,
    resolve_image_id_from_test_log,
)
from ..workspace import iteration_id_for_index, resolve_k8s_experiment_id
from .config import BaselineCodeMode, RunConfig, SampleContext


def build_run_config(
    *,
    timeout: int,
    force: bool,
    k8s_iteration: str | None,
    k8s_iterations: int,
    k8s_wait_timeout: int,
    k8s_refinement: str | None,
    ft_timeout: int | None,
    num_ports: int,
    min_port: int,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    vllm_port: int,
    baseline_code_mode: BaselineCodeMode = "reuse",
    baseline_code_max_attempts: int = 3,
    baseline_spec_max_attempts: int = 5,
) -> RunConfig:
    """Resolve env-derived knobs (load profile, refinement mode, iteration plan)."""
    iteration_ids = _plan_iteration_ids(
        num_refinement_iterations=k8s_iterations,
        explicit_iteration=k8s_iteration
        or os.environ.get("BAXBENCH_K8S_ITERATION")
        or None,
    )
    return RunConfig(
        load_profile=os.environ.get("BAXBENCH_LOAD_PROFILE", "quick-check"),
        experiment_id=resolve_k8s_experiment_id(),
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
        vllm_port=vllm_port,
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        force=force,
        baseline_code_mode=baseline_code_mode,
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
    """Gate on FT, resolve/build the base docker image. ``None`` on skip.

    In ``baseline_code_mode='regenerate'``, baseline code generation has
    already run by the time we get here — bypass the sample-level FT gate
    (it would only check ``--mode test`` artifacts, which may be missing or
    stale relative to the regenerated app) and instead use the iteration-000
    snapshot + FT log produced by :func:`regenerate_baseline_sample_preflight`.
    """
    if cfg.baseline_code_mode == "regenerate":
        return regenerate_baseline_sample_preflight(task, results_dir, sample, cfg)

    save_dir = task.get_save_dir(results_dir)
    if not functional_tests_gate(task, results_dir, sample):
        # Add a hint pointing to the regenerate mode — the existing skip line
        # already explains *what* failed, this one explains *how to bypass it*.
        append_k8s_skip(
            save_dir,
            sample,
            "hint: pass --baseline-code regenerate to generate fresh code "
            "with the current prompt and run FTs against it before k8s bench",
        )
        return None

    sample_dir = task.get_sample_dir(results_dir, sample)
    image_id = resolve_image_id_from_test_log(task, results_dir, sample)
    if image_id is None:
        test_log = task.get_functional_tests_dir(results_dir, sample) / "test.log"
        append_k8s_skip(
            save_dir,
            sample,
            f"skipped: no docker image id found in {test_log}",
        )
        return None

    sample_logger = logging.getLogger(task.id)
    image_id = ensure_docker_image(
        task,
        results_dir,
        sample,
        image_id,
        sample_logger,
        code_dir=task.get_code_dir(results_dir, sample),
    )
    if image_id is None:
        append_k8s_skip(
            save_dir,
            sample,
            "skipped: failed to build docker image from sample code",
        )
        return None

    return SampleContext(
        task=task,
        results_dir=results_dir,
        sample=sample,
        sample_dir=sample_dir,
        save_dir=save_dir,
        base_image_id=image_id,
    )


def regenerate_baseline_sample_preflight(
    task: Any,
    results_dir: Path,
    sample: int,
    cfg: RunConfig,
) -> SampleContext | None:
    """
    Preflight when ``--baseline-code regenerate`` is set.

    Runs :func:`k8s_bench.baseline.codegen.run_baseline_codegen` to (re)generate
    application code for iteration-000 with up to ``cfg.baseline_code_max_attempts``
    FT-validated tries. On success the resulting iteration-local code dir and
    its docker image become the :class:`SampleContext` baseline used by
    iteration-000's spec/deploy/bench stages — exactly as if the sample had a
    passing ``--mode test`` artifact, but driven by the model + prompt of
    *this* experiment run.
    """
    from ..baseline.codegen import run_baseline_codegen

    save_dir = task.get_save_dir(results_dir)
    sample_dir = task.get_sample_dir(results_dir, sample)

    result = run_baseline_codegen(
        task=task,
        results_dir=results_dir,
        sample=sample,
        sample_dir=sample_dir,
        save_dir=save_dir,
        max_attempts=cfg.baseline_code_max_attempts,
        ft_timeout=cfg.ft_timeout,
        num_ports=cfg.num_ports,
        min_port=cfg.min_port,
        vllm_port=cfg.vllm_port,
        max_retries=cfg.max_retries,
        base_delay=cfg.base_delay,
        max_delay=cfg.max_delay,
        force=cfg.force,
    )
    if result is None:
        # ``run_baseline_codegen`` already logged the skip + marked the
        # iteration folder as ``-baseline-failed``; nothing left to do here.
        return None

    return SampleContext(
        task=task,
        results_dir=results_dir,
        sample=sample,
        sample_dir=sample_dir,
        save_dir=save_dir,
        base_image_id=result.image_id,
    )


def sample_postlude(ctx: SampleContext) -> None:
    """Refresh LLM cost summary and clean up baxbench namespaces."""
    sample_logger = logging.getLogger(ctx.task.id)
    try:
        refresh_k8s_cost_summary(ctx.sample_dir)
    except Exception as exc:
        sample_logger.warning("Could not refresh LLM cost summary: %s", exc)

    try:
        cleanup_baxbench_namespaces_after_bench(logger=sample_logger)
    except Exception as exc:
        sample_logger.warning("Post-bench namespace cleanup failed: %s", exc)

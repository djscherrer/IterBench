"""
Top-level entry points for the k8s benchmark loop.

Two flavours:

- :func:`run_iterative_k8s_bench` — full per-sample iterative bench (baseline +
  N refinements). Delegates to :mod:`k8s_bench.orchestration` and
  :mod:`k8s_bench.stages`; this file only owns the outer loop shape.
- :func:`run_deploy_only_k8s_bench` — deploy + locust against existing iteration
  folders without spec generation or refinement.

``bench_k8s_for_task`` dispatches between the two based on ``k8s_spec_gen``.

The bulk of the iteration logic lives in:

- ``orchestration/`` — config, preflight, plan, execute
- ``stages/``        — code, spec, bench, outcome
- ``workspace/``     — paths, layout, meta, artifact I/O
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .iteration import resolve_iterations_to_run
from .orchestration.execute import execute_iteration
from .orchestration.preflight import (
    build_run_config,
    deploy_only_preflight,
    sample_postlude,
    sample_preflight,
)
from .stages.bench import run_locust_for_iteration
from .util.sample import append_k8s_skip
from .workspace import (
    bench_dir_has_complete_run,
    ensure_iteration_core_layout,
    iteration_bench_dir,
    iteration_code_snapshot_dir,
    latest_code_dir,
)


def find_latest_perf_run_dir(
    sample_dir: Path,
    iteration_id: str,
    load_profile: str,
) -> Path | None:
    """Back-compat shim: locate the most recent bench dir for an iteration."""
    del load_profile
    return resolve_bench_dir(sample_dir, iteration_id)


def run_iterative_k8s_bench(
    task: Any,
    results_dir: Path,
    samples: list[int],
    timeout: int,
    force: bool,
    *,
    k8s_iteration: str | None = None,
    k8s_iterations: int = 1,
    k8s_wait_timeout: int = 300,
    k8s_refinement: str | None = None,
    ft_timeout: int | None = None,
    num_ports: int = 10000,
    min_port: int = 12345,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
    baseline_code_mode: str = "reuse",
    baseline_code_max_attempts: int = 3,
    baseline_spec_max_attempts: int = 5,
) -> list[Path]:
    cfg = build_run_config(
        timeout=timeout,
        force=force,
        k8s_iteration=k8s_iteration,
        k8s_iterations=k8s_iterations,
        k8s_wait_timeout=k8s_wait_timeout,
        k8s_refinement=k8s_refinement,
        ft_timeout=ft_timeout,
        num_ports=num_ports,
        min_port=min_port,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        vllm_port=vllm_port,
        baseline_code_mode=baseline_code_mode,  # type: ignore[arg-type]
        baseline_code_max_attempts=baseline_code_max_attempts,
        baseline_spec_max_attempts=baseline_spec_max_attempts,
    )
    run_dirs_created: list[Path] = []

    for sample in samples:
        ctx = sample_preflight(task, results_dir, sample, cfg)
        if ctx is None:
            continue

        for iteration_index, iteration_id in enumerate(cfg.iteration_ids):
            outcome = execute_iteration(ctx, iteration_index, iteration_id, cfg)
            if outcome.run_dir is not None:
                run_dirs_created.append(outcome.run_dir)
            if outcome.abort_sample:
                break

        sample_postlude(ctx)

    return run_dirs_created


def run_deploy_only_k8s_bench(
    task: Any,
    results_dir: Path,
    samples: list[int],
    timeout: int,
    force: bool,
    *,
    k8s_iteration: str | None = None,
    k8s_iteration_path: Path | None = None,
    k8s_wait_timeout: int = 300,
    k8s_auto_init: bool = False,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
) -> list[Path]:
    """Run Locust against existing iteration folders. No spec generation."""
    run_dirs_created: list[Path] = []
    load_profile = os.environ.get("BAXBENCH_LOAD_PROFILE", "default")
    k8s_iteration = k8s_iteration or os.environ.get("BAXBENCH_K8S_ITERATION") or None
    deploy_cfg = build_run_config(
        timeout=timeout,
        force=force,
        k8s_iteration=k8s_iteration,
        k8s_iterations=0,
        k8s_wait_timeout=k8s_wait_timeout,
        k8s_refinement="off",
        ft_timeout=None,
        num_ports=10000,
        min_port=12345,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        vllm_port=8000,
    )
    # Override the load profile after the fact (deploy-only uses ``default``
    # rather than ``quick-check``); the config is otherwise the same.
    from dataclasses import replace

    deploy_cfg = replace(deploy_cfg, load_profile=load_profile)

    for sample in samples:
        sample_dir = task.get_sample_dir(results_dir, sample)
        save_dir = task.get_save_dir(results_dir)

        try:
            iteration_paths = resolve_iterations_to_run(
                sample_dir,
                iteration_id=k8s_iteration,
                auto_init=k8s_auto_init,
                iteration_path=k8s_iteration_path,
            )
        except FileNotFoundError as exc:
            append_k8s_skip(save_dir, sample, f"skipped: {exc}")
            continue

        if k8s_iteration_path is not None:
            ctx = deploy_only_preflight(
                task, results_dir, sample, iteration_paths[0], deploy_cfg
            )
        else:
            ctx = sample_preflight(task, results_dir, sample, deploy_cfg)
        if ctx is None:
            continue

        for iteration_path in iteration_paths:
            iteration_id = iteration_path.name
            bench_dir = iteration_bench_dir(iteration_path)
            already_benched = bench_dir_has_complete_run(bench_dir)
            if not force and already_benched:
                append_k8s_skip(
                    ctx.save_dir,
                    ctx.sample,
                    f"skipped: k8s perf run already exists for "
                    f"iteration={iteration_id!r} load_profile={load_profile!r}",
                )
                continue

            ensure_iteration_core_layout(iteration_path)
            run_dir = bench_dir
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dirs_created.append(run_dir)
            log_file = run_dir / "bench.log"

            baseline_code = task.get_code_dir(results_dir, sample)
            code_snap = iteration_code_snapshot_dir(iteration_path)
            if code_snap.is_dir() and any(code_snap.iterdir()):
                source_code_dir = code_snap
            else:
                source_code_dir = latest_code_dir(
                    ctx.sample_dir,
                    fallback=baseline_code,
                )

            with task.create_logger(log_file) as logger:
                run_locust_for_iteration(
                    task,
                    results_dir,
                    sample,
                    iteration_path,
                    run_dir,
                    ctx.base_image_id,
                    timeout=timeout,
                    bench_users=bench_users,
                    bench_spawn_rate=bench_spawn_rate,
                    bench_run_time=bench_run_time,
                    k8s_wait_timeout=k8s_wait_timeout,
                    logger=logger,
                    rebuild_code_dir=source_code_dir,
                )
                try:
                    from .experiment_summary import append_perf_run_block

                    append_perf_run_block(
                        sample_dir=ctx.sample_dir,
                        iteration_id=iteration_id,
                        perf_run_dir=run_dir,
                        load_profile=load_profile,
                    )
                except Exception as sum_exc:
                    logger.warning(
                        "Could not update experiment summary: %s", sum_exc
                    )
                logger.info(
                    "finished k8s bench sample=%d iteration=%s",
                    sample,
                    iteration_id,
                )

        sample_postlude(ctx)

    return run_dirs_created


def bench_k8s_for_task(
    task: Any,
    results_dir: Path,
    samples: list[int],
    timeout: int,
    force: bool,
    *,
    k8s_iteration: str | None = None,
    k8s_iteration_path: Path | None = None,
    k8s_iterations: int = 1,
    k8s_spec_gen: bool = True,
    k8s_wait_timeout: int = 300,
    k8s_auto_init: bool = False,
    k8s_refinement: str | None = None,
    ft_timeout: int | None = None,
    num_ports: int = 10000,
    min_port: int = 12345,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
    baseline_code_mode: str = "reuse",
    baseline_code_max_attempts: int = 3,
    baseline_spec_max_attempts: int = 5,
) -> list[Path]:
    if k8s_spec_gen:
        return run_iterative_k8s_bench(
            task,
            results_dir,
            samples,
            timeout,
            force,
            k8s_iteration=k8s_iteration,
            k8s_iterations=k8s_iterations,
            k8s_wait_timeout=k8s_wait_timeout,
            k8s_refinement=k8s_refinement,
            ft_timeout=ft_timeout,
            num_ports=num_ports,
            min_port=min_port,
            bench_users=bench_users,
            bench_spawn_rate=bench_spawn_rate,
            bench_run_time=bench_run_time,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            vllm_port=vllm_port,
            baseline_code_mode=baseline_code_mode,
            baseline_code_max_attempts=baseline_code_max_attempts,
            baseline_spec_max_attempts=baseline_spec_max_attempts,
        )
    return run_deploy_only_k8s_bench(
        task,
        results_dir,
        samples,
        timeout,
        force,
        k8s_iteration=k8s_iteration,
        k8s_iteration_path=k8s_iteration_path,
        k8s_wait_timeout=k8s_wait_timeout,
        k8s_auto_init=k8s_auto_init,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
    )

"""
K8s benchmark entry point: :func:`run_k8s_bench`.

Two modes, selected by ``deploy_only``:

- **Iterative experiment** (default): LLM baseline codegen, spec generation,
  refinement decisions, deploy, and Locust — via :mod:`k8s_bench.orchestration`
  and :mod:`k8s_bench.stages`.
- **Deploy-only**: deploy + Locust against existing ``iterations/…`` folders
  (hand-edited or copied code/spec); no LLM stages.

Orchestration detail lives in ``orchestration/``, ``stages/``, ``workspace/``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import tqdm

from .iteration import resolve_iterations_to_run
from .orchestration.execute import execute_iteration
from .orchestration.preflight import (
    build_run_config,
    deploy_only_preflight,
    sample_context_from_baseline_disk,
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
    k8s_fallback_code_dir,
    latest_code_dir,
    resolve_bench_dir,
)


def run_k8s_bench(
    tasks: list[Any],
    results_dir: Path,
    samples: list[int],
    *,
    deploy_only: bool = False,
    timeout: int,
    force: bool,
    k8s_cluster: str,
    k8s_iteration: str | None = None,
    k8s_iteration_path: Path | None = None,
    k8s_iterations: int = 1,
    k8s_wait_timeout: int = 300,
    k8s_auto_init: bool = False,
    k8s_refinement: str = "auto",
    load_profile: str = "quick-check",
    k8s_experiment_id: str | None = None,
    llm_max_cost_usd: float | None = None,
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
    baseline_code_max_attempts: int = 3,
    baseline_spec_max_attempts: int = 5,
) -> list[Path]:
    """Run k8s-bench for every task × sample (with tqdm progress)."""
    total = len(tasks) * max(1, len(samples))
    run_dirs: list[Path] = []
    with tqdm.tqdm(total=total) as pbar:
        for task in tasks:
            mode_label = "deploy-only" if deploy_only else "iterate"
            for si, sample in enumerate(samples):
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.set_description(
                        f"k8s/{mode_label} {task.model} - {task.scenario.id} - "
                        f"{task.env.id} - openhands={task.use_openhands} - "
                        f"sample {si + 1}/{len(samples)}"
                    )
                if deploy_only:
                    run_dirs.extend(
                        _run_deploy_only_for_task(
                            task,
                            results_dir,
                            [sample],
                            timeout=timeout,
                            force=force,
                            k8s_cluster=k8s_cluster,
                            k8s_iteration=k8s_iteration,
                            k8s_iteration_path=k8s_iteration_path,
                            k8s_wait_timeout=k8s_wait_timeout,
                            k8s_auto_init=k8s_auto_init,
                            k8s_refinement=k8s_refinement,
                            load_profile=load_profile,
                            k8s_experiment_id=k8s_experiment_id,
                            llm_max_cost_usd=llm_max_cost_usd,
                            bench_users=bench_users,
                            bench_spawn_rate=bench_spawn_rate,
                            bench_run_time=bench_run_time,
                        )
                    )
                else:
                    run_dirs.extend(
                        _run_iterative_experiment_for_task(
                            task,
                            results_dir,
                            [sample],
                            timeout=timeout,
                            force=force,
                            k8s_cluster=k8s_cluster,
                            k8s_iteration=k8s_iteration,
                            k8s_iterations=k8s_iterations,
                            k8s_wait_timeout=k8s_wait_timeout,
                            k8s_refinement=k8s_refinement,
                            load_profile=load_profile,
                            k8s_experiment_id=k8s_experiment_id,
                            llm_max_cost_usd=llm_max_cost_usd,
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
                            baseline_code_max_attempts=baseline_code_max_attempts,
                            baseline_spec_max_attempts=baseline_spec_max_attempts,
                        )
                    )
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.update(1)
    return run_dirs


def _run_iterative_experiment_for_task(
    task: Any,
    results_dir: Path,
    samples: list[int],
    *,
    timeout: int,
    force: bool,
    k8s_cluster: str,
    k8s_iteration: str | None,
    k8s_iterations: int,
    k8s_wait_timeout: int,
    k8s_refinement: str,
    load_profile: str,
    k8s_experiment_id: str | None,
    llm_max_cost_usd: float | None,
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
    baseline_code_max_attempts: int,
    baseline_spec_max_attempts: int,
) -> list[Path]:
    cfg = build_run_config(
        timeout=timeout,
        force=force,
        k8s_cluster=k8s_cluster,
        k8s_iteration=k8s_iteration,
        k8s_iterations=k8s_iterations,
        k8s_wait_timeout=k8s_wait_timeout,
        k8s_refinement=k8s_refinement,
        load_profile=load_profile,
        k8s_experiment_id=k8s_experiment_id,
        llm_max_cost_usd=llm_max_cost_usd,
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
        baseline_code_max_attempts=baseline_code_max_attempts,
        baseline_spec_max_attempts=baseline_spec_max_attempts,
    )
    run_dirs: list[Path] = []

    for sample in samples:
        ctx = sample_preflight(task, results_dir, sample, cfg)
        if ctx is None:
            continue

        for iteration_index, iteration_id in enumerate(cfg.iteration_ids):
            outcome = execute_iteration(ctx, iteration_index, iteration_id, cfg)
            if outcome.run_dir is not None:
                run_dirs.append(outcome.run_dir)
            if outcome.base_image_id is not None:
                ctx = replace(ctx, base_image_id=outcome.base_image_id)
            if outcome.abort_sample:
                break

        sample_postlude(ctx)

    return run_dirs


def _run_deploy_only_for_task(
    task: Any,
    results_dir: Path,
    samples: list[int],
    *,
    timeout: int,
    force: bool,
    k8s_cluster: str,
    k8s_iteration: str | None,
    k8s_iteration_path: Path | None,
    k8s_wait_timeout: int,
    k8s_auto_init: bool,
    k8s_refinement: str,
    load_profile: str,
    k8s_experiment_id: str | None,
    llm_max_cost_usd: float | None,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
) -> list[Path]:
    """Deploy + Locust for existing iteration folders; no LLM stages."""
    run_dirs: list[Path] = []
    cfg = build_run_config(
        timeout=timeout,
        force=force,
        k8s_cluster=k8s_cluster,
        k8s_iteration=k8s_iteration,
        k8s_iterations=0,
        k8s_wait_timeout=k8s_wait_timeout,
        k8s_refinement=k8s_refinement,
        load_profile=load_profile,
        k8s_experiment_id=k8s_experiment_id,
        llm_max_cost_usd=llm_max_cost_usd,
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

    for sample in samples:
        sample_dir = task.get_sample_dir(results_dir, sample)
        task_run_dir = task.get_save_dir(results_dir)

        try:
            iteration_paths = resolve_iterations_to_run(
                sample_dir,
                iteration_id=k8s_iteration,
                auto_init=k8s_auto_init,
                iteration_path=k8s_iteration_path,
            )
        except FileNotFoundError as exc:
            append_k8s_skip(task_run_dir, sample, f"skipped: {exc}")
            continue

        if k8s_iteration_path is not None:
            ctx = deploy_only_preflight(
                task, results_dir, sample, iteration_paths[0], cfg
            )
        else:
            ctx = sample_context_from_baseline_disk(
                task, results_dir, sample, cfg
            )
        if ctx is None:
            continue

        for iteration_path in iteration_paths:
            iteration_id = iteration_path.name
            bench_dir = iteration_bench_dir(iteration_path)
            if not force and bench_dir_has_complete_run(bench_dir):
                append_k8s_skip(
                    ctx.task_run_dir,
                    ctx.sample,
                    f"skipped: k8s perf run already exists for "
                    f"iteration={iteration_id!r} load_profile={load_profile!r}",
                )
                continue

            ensure_iteration_core_layout(iteration_path)
            run_dir = bench_dir
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dirs.append(run_dir)
            log_file = run_dir / "bench.log"

            code_snap = iteration_code_snapshot_dir(iteration_path)
            if code_snap.is_dir() and any(code_snap.iterdir()):
                source_code_dir = code_snap
            else:
                source_code_dir = latest_code_dir(
                    ctx.sample_dir,
                    fallback=k8s_fallback_code_dir(ctx.sample_dir),
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
                    load_profile=cfg.load_profile,
                    k8s_cluster=cfg.k8s_cluster,
                    logger=logger,
                    rebuild_code_dir=source_code_dir,
                )
                try:
                    from .experiment_summary import append_perf_run_block

                    append_perf_run_block(
                        sample_dir=ctx.sample_dir,
                        iteration_id=iteration_id,
                        perf_run_dir=run_dir,
                        load_profile=cfg.load_profile,
                    )
                except Exception as sum_exc:
                    logger.warning(
                        "Could not update experiment summary: %s", sum_exc
                    )
                logger.info(
                    "finished deploy-only bench sample=%d iteration=%s",
                    sample,
                    iteration_id,
                )

        sample_postlude(ctx)

    return run_dirs


def find_latest_perf_run_dir(
    sample_dir: Path,
    iteration_id: str,
    load_profile: str,
) -> Path | None:
    """Locate the most recent bench dir for an iteration (legacy helper)."""
    del load_profile
    return resolve_bench_dir(sample_dir, iteration_id)

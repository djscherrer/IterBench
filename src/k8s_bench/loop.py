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

from .orchestration.deploy_only import execute_deploy_only_iteration
from .orchestration.execute import execute_iteration
from .orchestration.preflight import (
    build_run_config,
    deploy_only_preflight,
    sample_context_from_baseline_disk,
    sample_postlude,
    sample_preflight,
)
from .workspace.skips import append_k8s_skip
from .workspace import (
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    iterations_root,
    list_iteration_dirs,
    new_iteration_id,
    resolve_bench_dir,
    resolve_iteration_dir,
)


def resolve_iterations_to_run(
    sample_dir: Path,
    *,
    iteration_id: str | None,
    auto_init: bool,
    iteration_path: Path | None = None,
    experiment_id: str | None = None,
) -> list[Path]:
    """Resolve which iteration directories to deploy/bench in deploy-only mode."""
    if iteration_path is not None:
        path = Path(iteration_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Iteration path is not a directory: {path}")
        if find_iteration_spec_path(path) is None:
            raise FileNotFoundError(f"Missing spec for iteration: {path}")
        return [path]
    if iteration_id:
        path = resolve_iteration_dir(
            sample_dir, iteration_id, experiment_id=experiment_id
        )
        if find_iteration_spec_path(path) is None:
            raise FileNotFoundError(f"Missing spec for iteration: {path}")
        return [path]
    existing = list_iteration_dirs(sample_dir, experiment_id=experiment_id)
    if existing:
        return existing
    if not auto_init:
        raise FileNotFoundError(
            f"No k8s iterations under {iterations_root(sample_dir, experiment_id=experiment_id)}; "
            "pass --k8s-iteration or enable auto-init."
        )
    iid = new_iteration_id(sample_dir, experiment_id=experiment_id)
    path = resolve_iteration_dir(
        sample_dir, iid, experiment_id=experiment_id
    )
    ensure_iteration_core_layout(path)
    return [path]


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
                        f"{task.env.id} - sample {si + 1}/{len(samples)}"
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
        baseline_code_max_attempts=baseline_code_max_attempts,
        baseline_spec_max_attempts=baseline_spec_max_attempts,
    )
    run_dirs: list[Path] = []

    for sample in samples:
        ctx = sample_preflight(task, results_dir, sample, cfg)
        if ctx is None:
            continue

        sample_run_dirs: list[Path] = []
        for iteration_index, iteration_id in enumerate(cfg.iteration_ids):
            outcome = execute_iteration(ctx, iteration_index, iteration_id, cfg)
            if outcome.run_dir is not None:
                sample_run_dirs.append(outcome.run_dir)
                run_dirs.append(outcome.run_dir)
            if outcome.base_image_id is not None:
                ctx = replace(ctx, base_image_id=outcome.base_image_id)
            if outcome.abort_sample:
                break

        # Only tear down cluster namespaces when this sample actually ran a bench.
        # Full-skip re-runs (finished success/failed iterations) should not block on
        # ``kubectl delete namespace --wait`` for leftover namespaces.
        sample_postlude(ctx, cleanup_namespaces=bool(sample_run_dirs))

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
                experiment_id=cfg.experiment_id,
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
            run_dir = execute_deploy_only_iteration(ctx, iteration_path, cfg)
            if run_dir is not None:
                run_dirs.append(run_dir)

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

"""Tqdm over all BaxBench tasks × samples; delegates to ``loop`` or ``spec.generation``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tqdm

from .loop import bench_k8s_for_task
from .spec.generation import generate_k8s_specs_for_task
from .util.sample import functional_tests_gate


def run_k8s_bench(
    tasks: list[Any],
    results_dir: Path,
    samples: list[int],
    timeout: int,
    force: bool,
    *,
    k8s_iteration: str | None = None,
    k8s_iterations: int = 1,
    k8s_spec_gen: bool = True,
    k8s_wait_timeout: int = 300,
    k8s_auto_init: bool = False,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
) -> list[Path]:
    total = len(tasks) * max(1, len(samples))
    all_paths: list[Path] = []
    with tqdm.tqdm(total=total) as pbar:
        for task in tasks:
            model_label = f"{task.model}"
            env_label = task.env.id
            scenario_label = task.scenario.id
            openhands_label = "true" if task.use_openhands else "false"
            for si, sample in enumerate(samples):
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.set_description(
                        f"k8s {model_label} - {scenario_label} - {env_label} - openhands={openhands_label} - sample {si + 1}/{len(samples)}"
                    )
                all_paths.extend(
                    bench_k8s_for_task(
                        task,
                        results_dir,
                        [sample],
                        timeout,
                        force,
                        k8s_iteration=k8s_iteration,
                        k8s_iterations=k8s_iterations,
                        k8s_spec_gen=k8s_spec_gen,
                        k8s_wait_timeout=k8s_wait_timeout,
                        k8s_auto_init=k8s_auto_init,
                        bench_users=bench_users,
                        bench_spawn_rate=bench_spawn_rate,
                        bench_run_time=bench_run_time,
                        max_retries=max_retries,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        vllm_port=vllm_port,
                    )
                )
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.update(1)
    return all_paths


def run_k8s_spec_gen(
    tasks: list[Any],
    results_dir: Path,
    samples: list[int],
    force: bool,
    *,
    k8s_iteration: str | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
) -> list[Path]:
    total = len(tasks) * max(1, len(samples))
    all_paths: list[Path] = []
    with tqdm.tqdm(total=total) as pbar:
        for task in tasks:
            model_label = f"{task.model}"
            env_label = task.env.id
            scenario_label = task.scenario.id
            for si, sample in enumerate(samples):
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.set_description(
                        f"k8s-spec {model_label} - {scenario_label} - {env_label} - sample {si + 1}/{len(samples)}"
                    )
                if not functional_tests_gate(task, results_dir, sample):
                    continue
                all_paths.extend(
                    generate_k8s_specs_for_task(
                        task,
                        results_dir,
                        [sample],
                        force,
                        k8s_iteration=k8s_iteration,
                        max_retries=max_retries,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        vllm_port=vllm_port,
                    )
                )
                with pbar.get_lock():  # type: ignore[no-untyped-call]
                    pbar.update(1)
    return all_paths

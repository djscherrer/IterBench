"""
Bench policy for one BaxBench task: phases, skips, spec generation, feedback.

Call stack (see also ``handler.py`` and ``iteration.py``)::

    handler.run_k8s_bench          # tqdm over tasks × samples
      → bench_k8s_for_task         # iterative vs deploy-only
        → run_iterative_k8s_bench  # per sample: phases
          → generate_k8s_specs_for_task   # spec/generation.py (LLM)
          → run_locust_for_iteration      # iteration.py (kubectl + Locust)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .iteration import resolve_iterations_to_run, run_k8s_bench_iteration
from .spec.models import K8sWorkloadSpec
from .paths import (
    iteration_id_for_phase,
    k8s_workspace_root,
    normalize_iteration_id,
    resolve_k8s_experiment_id,
)
from .spec.generation import generate_k8s_specs_for_task
from .util.sample import (
    append_k8s_skip,
    bench_labels,
    ensure_docker_image,
    functional_tests_gate,
    performance_test_names,
    resolve_image_id_from_test_log,
    resolve_locustfile,
)
from .feedback import (
    collect_iteration_feedback,
    load_feedback_from_run_dir,
    write_feedback_artifact,
)


def plan_iteration_phases(
    *,
    num_iterations: int,
    explicit_iteration: str | None = None,
) -> list[str]:
    if explicit_iteration:
        return [normalize_iteration_id(explicit_iteration)]
    if num_iterations < 1:
        raise ValueError("num_iterations must be >= 1")
    return [iteration_id_for_phase(i) for i in range(1, num_iterations + 1)]


def find_latest_perf_run_dir(
    sample_dir: Path,
    iteration_id: str,
    load_profile: str,
) -> Path | None:
    iid = normalize_iteration_id(iteration_id)
    safe_profile = re.sub(r"[^a-zA-Z0-9_-]+", "-", load_profile.strip()) or "default"
    pattern = f"perf-k8s-{iid}-{safe_profile}-*"
    workspace = k8s_workspace_root(sample_dir)
    candidates = [
        p for p in workspace.glob(pattern) if p.is_dir() and (p / "bench.log").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


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
    phase_index: int | None = None,
    logger: logging.Logger,
) -> bool:
    from tasks import esc

    save_dir = task.get_save_dir(results_dir)
    iteration_id = iteration_path.name
    tests = performance_test_names(task)
    if not tests:
        append_k8s_skip(
            save_dir,
            sample,
            f"skipped phase {iteration_id}: no performance tests"
            if phase_index
            else "skipped: no performance tests configured",
        )
        return False

    image_id = ensure_docker_image(task, results_dir, sample, image_id, logger)
    if image_id is None:
        append_k8s_skip(
            save_dir,
            sample,
            f"skipped phase {iteration_id}: failed to build docker image"
            if phase_index
            else "skipped: failed to build docker image for k8s bench",
        )
        return False

    locustfile = resolve_locustfile(task, run_dir)
    if locustfile is None:
        append_k8s_skip(save_dir, sample, "skipped: missing locustfile")
        return False

    sample_slug = (
        f"{esc(task.model)}-{esc(task.env.id)}-{esc(task.scenario.id)}-sample{sample}"
    )
    labels = bench_labels(task, phase_index=phase_index)

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
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
) -> list[Path]:
    run_dirs_created: list[Path] = []
    load_profile = os.environ.get("BAXBENCH_LOAD_PROFILE", "quick-check")
    experiment_id = resolve_k8s_experiment_id()
    phase_ids = plan_iteration_phases(
        num_iterations=k8s_iterations,
        explicit_iteration=k8s_iteration or os.environ.get("BAXBENCH_K8S_ITERATION") or None,
    )

    for sample in samples:
        save_dir = task.get_save_dir(results_dir)
        if not functional_tests_gate(task, results_dir, sample):
            continue

        sample_dir = task.get_sample_dir(results_dir, sample)
        image_id = resolve_image_id_from_test_log(task, results_dir, sample)
        if image_id is None:
            test_log = task.get_functional_tests_dir(results_dir, sample) / "test.log"
            append_k8s_skip(
                save_dir,
                sample,
                f"skipped: no docker image id found in {test_log}",
            )
            continue

        prior_feedback: Any | None = None

        for phase_index, iteration_id in enumerate(phase_ids, start=1):
            iteration_path = task.get_k8s_iteration_dir(results_dir, sample, iteration_id)

            if not force and task.has_k8s_perf_run_for_iteration(
                sample_dir,
                iteration_id=iteration_id,
                load_profile=load_profile,
            ):
                append_k8s_skip(
                    save_dir,
                    sample,
                    f"skipped phase {iteration_id}: perf run already exists (load_profile={load_profile!r})",
                )
                prev_run = find_latest_perf_run_dir(
                    sample_dir, iteration_id, load_profile
                )
                if prev_run is not None:
                    prior_feedback = load_feedback_from_run_dir(prev_run)
                    if prior_feedback is None:
                        prior_feedback = collect_iteration_feedback(
                            perf_run_dir=prev_run,
                            iteration_path=iteration_path,
                        )
                continue

            run_dir = task.get_k8s_bench_run_dir(results_dir, sample, iteration_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dirs_created.append(run_dir)
            log_file = run_dir / "bench.log"

            with task.create_logger(log_file) as logger:
                logger.info(
                    "k8s iterative phase %d/%d experiment=%s iteration=%s workspace=%s",
                    phase_index,
                    len(phase_ids),
                    experiment_id or "(legacy)",
                    iteration_id,
                    k8s_workspace_root(sample_dir),
                )

                generate_k8s_specs_for_task(
                    task,
                    results_dir,
                    [sample],
                    force,
                    k8s_iteration=iteration_id,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    max_delay=max_delay,
                    vllm_port=vllm_port,
                    prior_feedback=prior_feedback,
                    phase_index=phase_index,
                )

                if not iteration_path.joinpath("spec.yaml").is_file():
                    append_k8s_skip(
                        save_dir,
                        sample,
                        f"skipped phase {iteration_id}: spec generation did not produce spec.yaml",
                    )
                    continue

                run_locust_for_iteration(
                    task,
                    results_dir,
                    sample,
                    iteration_path,
                    run_dir,
                    image_id,
                    timeout=timeout,
                    bench_users=bench_users,
                    bench_spawn_rate=bench_spawn_rate,
                    bench_run_time=bench_run_time,
                    k8s_wait_timeout=k8s_wait_timeout,
                    phase_index=phase_index,
                    logger=logger,
                )

                try:
                    spec = K8sWorkloadSpec.from_yaml_file(iteration_path / "spec.yaml")
                    fb = collect_iteration_feedback(
                        perf_run_dir=run_dir,
                        iteration_path=iteration_path,
                        namespace=spec.namespace,
                        logger=logger,
                    )
                    write_feedback_artifact(run_dir, fb)
                    prior_feedback = fb
                    try:
                        from .experiment_summary import append_perf_run_block

                        summary_path = append_perf_run_block(
                            sample_dir=sample_dir,
                            iteration_id=iteration_id,
                            perf_run_dir=run_dir,
                            feedback=fb,
                            load_profile=load_profile,
                        )
                        logger.info("Updated experiment summary: %s", summary_path)
                    except Exception as sum_exc:
                        logger.warning(
                            "Could not update experiment summary: %s", sum_exc
                        )
                except Exception as exc:
                    logger.warning("Could not write iteration feedback: %s", exc)

            logging.getLogger(task.id).info(
                "finished k8s bench sample=%s iteration=%s", sample, iteration_id
            )

        try:
            from .cluster.cleanup import cleanup_baxbench_namespaces_after_bench

            cleanup_baxbench_namespaces_after_bench(
                logger=logging.getLogger(task.id)
            )
        except Exception as exc:
            logging.getLogger(task.id).warning(
                "Post-bench namespace cleanup failed: %s", exc
            )

    return run_dirs_created


def run_deploy_only_k8s_bench(
    task: Any,
    results_dir: Path,
    samples: list[int],
    timeout: int,
    force: bool,
    *,
    k8s_iteration: str | None = None,
    k8s_wait_timeout: int = 300,
    k8s_auto_init: bool = False,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
) -> list[Path]:
    run_dirs_created: list[Path] = []
    load_profile = os.environ.get("BAXBENCH_LOAD_PROFILE", "default")
    k8s_iteration = k8s_iteration or os.environ.get("BAXBENCH_K8S_ITERATION") or None

    for sample in samples:
        save_dir = task.get_save_dir(results_dir)
        if not functional_tests_gate(task, results_dir, sample):
            continue

        sample_dir = task.get_sample_dir(results_dir, sample)
        image_id = resolve_image_id_from_test_log(task, results_dir, sample)
        if image_id is None:
            test_log = task.get_functional_tests_dir(results_dir, sample) / "test.log"
            append_k8s_skip(
                save_dir,
                sample,
                f"skipped: no docker image id found in {test_log}",
            )
            continue

        try:
            iteration_paths = resolve_iterations_to_run(
                sample_dir,
                iteration_id=k8s_iteration,
                auto_init=k8s_auto_init,
            )
        except FileNotFoundError as exc:
            append_k8s_skip(save_dir, sample, f"skipped: {exc}")
            continue

        for iteration_path in iteration_paths:
            iteration_id = iteration_path.name
            if not force and task.has_k8s_perf_run_for_iteration(
                sample_dir,
                iteration_id=iteration_id,
                load_profile=load_profile,
            ):
                append_k8s_skip(
                    save_dir,
                    sample,
                    f"skipped: k8s perf run already exists for iteration={iteration_id!r} load_profile={load_profile!r}",
                )
                continue

            run_dir = task.get_k8s_bench_run_dir(results_dir, sample, iteration_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dirs_created.append(run_dir)
            log_file = run_dir / "bench.log"

            with task.create_logger(log_file) as logger:
                run_locust_for_iteration(
                    task,
                    results_dir,
                    sample,
                    iteration_path,
                    run_dir,
                    image_id,
                    timeout=timeout,
                    bench_users=bench_users,
                    bench_spawn_rate=bench_spawn_rate,
                    bench_run_time=bench_run_time,
                    k8s_wait_timeout=k8s_wait_timeout,
                    logger=logger,
                )
                try:
                    from .experiment_summary import append_perf_run_block

                    append_perf_run_block(
                        sample_dir=sample_dir,
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

        try:
            from .cluster.cleanup import cleanup_baxbench_namespaces_after_bench

            cleanup_baxbench_namespaces_after_bench(
                logger=logging.getLogger(task.id)
            )
        except Exception as exc:
            logging.getLogger(task.id).warning(
                "Post-bench namespace cleanup failed: %s", exc
            )

    return run_dirs_created


def bench_k8s_for_task(
    task: Any,
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
            bench_users=bench_users,
            bench_spawn_rate=bench_spawn_rate,
            bench_run_time=bench_run_time,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            vllm_port=vllm_port,
        )
    return run_deploy_only_k8s_bench(
        task,
        results_dir,
        samples,
        timeout,
        force,
        k8s_iteration=k8s_iteration,
        k8s_wait_timeout=k8s_wait_timeout,
        k8s_auto_init=k8s_auto_init,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
    )

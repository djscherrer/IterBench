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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .iteration import resolve_iterations_to_run, run_k8s_bench_iteration
from .spec.models import K8sWorkloadSpec
from .workspace import (
    apply_iteration_folder_suffix,
    archive_bench_dir_if_present,
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    init_iteration_meta,
    is_baseline_phase,
    iteration_bench_dir,
    iteration_id_for_phase,
    k8s_workspace_root,
    normalize_iteration_id,
    resolve_bench_dir,
    resolve_iteration_dir,
    resolve_k8s_experiment_id,
    update_iteration_meta,
)
from .gates import probe_iteration_deployable
from .iteration_failure import fail_iteration_phase
from .spec.generation import (
    generate_and_write_spec,
    generate_baseline_spec_until_deployable,
    reuse_deployment_spec_for_iteration,
)
from .util.sample import (
    append_k8s_skip,
    bench_labels,
    ensure_docker_image,
    functional_tests_gate,
    performance_test_names,
    resolve_image_id_from_test_log,
    resolve_locustfile,
)
from .workspace import latest_code_dir
from .feedback import (
    collect_iteration_feedback,
    load_feedback_from_run_dir,
    load_prior_feedback_for_phase,
    write_feedback_artifact,
)
from .refinement import (
    decide_refinement_action,
    refine_code_until_passing,
    resolve_refinement_mode,
)
from .refinement.decision import write_refinement_decision_artifact


def plan_iteration_phases(
    *,
    num_refinement_iterations: int,
    explicit_iteration: str | None = None,
) -> list[str]:
    """
    Return iteration ids for one experiment.

    ``num_refinement_iterations=N`` yields ``iteration-000`` (baseline) plus
    ``iteration-001`` … ``iteration-{N:03d}`` (N refinement phases).
    """
    if explicit_iteration:
        return [normalize_iteration_id(explicit_iteration)]
    if num_refinement_iterations < 0:
        raise ValueError("num_refinement_iterations must be >= 0")
    return [
        iteration_id_for_phase(i) for i in range(0, num_refinement_iterations + 1)
    ]


def find_latest_perf_run_dir(
    sample_dir: Path,
    iteration_id: str,
    load_profile: str,
) -> Path | None:
    del load_profile
    return resolve_bench_dir(sample_dir, iteration_id)


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
    rebuild_code_dir: Path | None = None,
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

    image_id = ensure_docker_image(
        task, results_dir, sample, image_id, logger, code_dir=rebuild_code_dir
    )
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
) -> list[Path]:
    run_dirs_created: list[Path] = []
    load_profile = os.environ.get("BAXBENCH_LOAD_PROFILE", "quick-check")
    experiment_id = resolve_k8s_experiment_id()
    refinement_mode = resolve_refinement_mode(k8s_refinement)
    functional_test_timeout = ft_timeout if ft_timeout is not None else timeout
    phase_ids = plan_iteration_phases(
        num_refinement_iterations=k8s_iterations,
        explicit_iteration=k8s_iteration or os.environ.get("BAXBENCH_K8S_ITERATION") or None,
    )
    total_phases = len(phase_ids)

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
            continue

        prior_feedback: Any | None = None

        for phase_index, iteration_id in enumerate(phase_ids):
            is_baseline = is_baseline_phase(phase_index)
            iteration_path = resolve_iteration_dir(sample_dir, iteration_id)
            if not iteration_path.is_dir() and not (
                iteration_path / "meta.json"
            ).is_file():
                iteration_path = task.get_k8s_iteration_dir(
                    results_dir, sample, iteration_id
                )
            ensure_iteration_core_layout(iteration_path)

            if not is_baseline:
                # Always reload from disk: this picks up failed iterations between
                # the last successful one and the current phase, so the agent sees
                # them as anti-examples instead of repeating the same broken change.
                prior_feedback = load_prior_feedback_for_phase(sample_dir, phase_index)

            based_on = (
                prior_feedback.iteration_id if prior_feedback is not None else None
            )
            init_iteration_meta(
                iteration_path,
                phase_index=phase_index,
                iteration_id=iteration_id,
                based_on_iteration=based_on,
            )
            update_iteration_meta(
                iteration_path,
                refinement_mode=refinement_mode,
            )

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

            phase_logger = logging.getLogger(task.id)
            refinement_action = "baseline" if is_baseline else "deployment"
            reuse_prior_spec = False
            spec_source_iteration: str | None = None

            if (
                not is_baseline
                and prior_feedback is not None
                and refinement_mode != "off"
            ):
                from .refinement.decision import RefinementDecision

                if refinement_mode == "code":
                    refinement_action = "code"
                    decision = RefinementDecision(
                        action="code",
                        rationale=f"Forced by refinement mode={refinement_mode!r}",
                        raw_response="",
                        phase_index=phase_index,
                        based_on_iteration=prior_feedback.iteration_id,
                    )
                    write_refinement_decision_artifact(iteration_path, decision)
                elif refinement_mode == "deployment":
                    refinement_action = "deployment"
                    decision = RefinementDecision(
                        action="deployment",
                        rationale=f"Forced by refinement mode={refinement_mode!r}",
                        raw_response="",
                        phase_index=phase_index,
                        based_on_iteration=prior_feedback.iteration_id,
                    )
                    write_refinement_decision_artifact(iteration_path, decision)
                else:
                    decision = decide_refinement_action(
                        task=task,
                        results_dir=results_dir,
                        sample=sample,
                        iteration_path=iteration_path,
                        prior_feedback=prior_feedback,
                        phase_index=phase_index,
                        next_iteration_id=iteration_id,
                        logger=phase_logger,
                        vllm_port=vllm_port,
                        max_retries=max_retries,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        total_phases=total_phases,
                    )
                    refinement_action = decision.action
                    write_refinement_decision_artifact(iteration_path, decision)

                update_iteration_meta(
                    iteration_path,
                    refinement_action=refinement_action,
                    based_on_iteration=prior_feedback.iteration_id,
                )

                try:
                    from .experiment_summary import append_refinement_decision_block

                    append_refinement_decision_block(
                        sample_dir=sample_dir,
                        iteration_id=iteration_id,
                        decision=decision,
                        load_profile=load_profile,
                    )
                except Exception as sum_exc:
                    phase_logger.warning(
                        "Could not update experiment summary (decision): %s",
                        sum_exc,
                    )

                if refinement_action == "code":
                    phase_logger.info(
                        "phase %s: will refine application code after folder setup",
                        iteration_id,
                    )

            elif not is_baseline and refinement_mode != "off":
                from .refinement.decision import RefinementDecision

                phase_logger.warning(
                    "phase %s: no benchmark feedback from prior iterations; "
                    "defaulting to spec tuning (%s mode)",
                    iteration_id,
                    refinement_mode,
                )
                decision = RefinementDecision(
                    action="deployment",
                    rationale=(
                        "No benchmark feedback from prior iterations; "
                        "defaulting to deployment/spec tuning."
                    ),
                    raw_response="",
                    phase_index=phase_index,
                    based_on_iteration="",
                )
                write_refinement_decision_artifact(iteration_path, decision)
                update_iteration_meta(
                    iteration_path,
                    refinement_action=refinement_action,
                )

            folder_kind = (
                "baseline"
                if is_baseline
                else ("code" if refinement_action == "code" else "spec")
            )
            iteration_path = apply_iteration_folder_suffix(
                iteration_path, folder_kind
            )
            update_iteration_meta(iteration_path, folder=iteration_path.name)

            if (
                not is_baseline
                and refinement_action == "code"
                and prior_feedback is not None
            ):
                # If a previous phase already tried code refinement and failed
                # functional tests, hand its `failure_report.json` to the next
                # attempt so it sees the exact tests + errors. Without this the
                # prompt is dominated by benchmark numbers and the same class
                # of bug (e.g. a wrong SQL bind count) gets re-introduced.
                from .refinement.code import find_latest_prior_failure_report

                prior_failure_report = find_latest_prior_failure_report(
                    sample_dir, current_phase=phase_index
                )
                if prior_failure_report is not None:
                    phase_logger.info(
                        "phase %s: prior code-refinement failure detected in %s "
                        "(%d/%d FT passed, failed=%s); will surface in prompt",
                        iteration_id,
                        prior_failure_report.iteration_id,
                        prior_failure_report.num_passed_ft,
                        prior_failure_report.num_total_ft,
                        [
                            ft.name for ft in prior_failure_report.failed_tests
                        ] or "(unknown)",
                    )

                new_image = refine_code_until_passing(
                    task=task,
                    results_dir=results_dir,
                    sample=sample,
                    iteration_path=iteration_path,
                    prior_feedback=prior_feedback,
                    logger=phase_logger,
                    ft_timeout=functional_test_timeout,
                    num_ports=num_ports,
                    min_port=min_port,
                    vllm_port=vllm_port,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    max_delay=max_delay,
                    max_codegen_attempts=1,
                    prior_failure_report=prior_failure_report,
                    phase_index=phase_index,
                    total_phases=total_phases,
                )
                if new_image is None:
                    fail_iteration_phase(
                        iteration_path=iteration_path,
                        save_dir=save_dir,
                        sample=sample,
                        iteration_id=iteration_id,
                        failure_reason=(
                            "Functional tests did not pass after code refinement"
                        ),
                        kind="code",
                        logger=phase_logger,
                    )
                    # The folder is now renamed `iteration-NNN-code-failed` and
                    # `failure_report.json` is on disk; that is the signal the
                    # next phase's decision LLM and code-refinement LLM read.
                    continue
                image_id = new_image
                reuse_prior_spec = True
                spec_source_iteration = prior_feedback.iteration_id
                update_iteration_meta(
                    iteration_path,
                    code_modified=True,
                )

            run_dir = iteration_bench_dir(iteration_path)
            if force:
                archived = archive_bench_dir_if_present(iteration_path)
                if archived is not None:
                    phase_logger.info(
                        "Archived previous bench run to %s", archived
                    )
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dirs_created.append(run_dir)
            log_file = run_dir / "bench.log"

            with task.create_logger(log_file) as logger:
                logger.info(
                    "k8s iterative phase %d/%d experiment=%s iteration=%s path=%s refinement=%s action=%s",
                    phase_index,
                    len(phase_ids) - 1,
                    experiment_id,
                    iteration_id,
                    iteration_path,
                    refinement_mode,
                    refinement_action,
                )

                from tasks import esc

                bench_labels_dict = {
                    "baxbench.dev/model": esc(task.model),
                    "baxbench.dev/scenario": esc(task.scenario.id),
                    "baxbench.dev/env": esc(task.env.id),
                    "baxbench.dev/spec-gen": "true",
                    "baxbench.dev/phase": str(phase_index),
                }
                sample_slug = (
                    f"{esc(task.model)}-{esc(task.env.id)}-"
                    f"{esc(task.scenario.id)}-sample{sample}"
                )

                spec_file: Path | None = None

                if is_baseline:
                    def _baseline_probe():
                        return probe_iteration_deployable(
                            iteration_path=iteration_path,
                            image_id=image_id,
                            sample_slug=sample_slug,
                            app_port=task.env.port,
                            needs_db=task.scenario.needs_db,
                            wait_timeout_s=k8s_wait_timeout,
                            labels=bench_labels_dict,
                            logger=logger,
                        )

                    spec_file, baseline_err = generate_baseline_spec_until_deployable(
                        task=task,
                        results_dir=results_dir,
                        sample=sample,
                        iteration_path=iteration_path,
                        iteration_id=iteration_id,
                        logger=logger,
                        deploy_probe=_baseline_probe,
                        phase_index=phase_index,
                        total_phases=total_phases,
                        vllm_port=vllm_port,
                    )
                    if spec_file is None:
                        fail_iteration_phase(
                            iteration_path=iteration_path,
                            save_dir=save_dir,
                            sample=sample,
                            iteration_id=iteration_id,
                            failure_reason=baseline_err
                            or "baseline spec never became deployable",
                            kind="baseline",
                            logger=logger,
                        )
                        break
                    update_iteration_meta(iteration_path, spec_regenerated=True)

                elif reuse_prior_spec and spec_source_iteration:
                    logger.info(
                        "phase %s: reusing deployment spec from %s (code refinement; "
                        "no LLM spec generation)",
                        iteration_id,
                        spec_source_iteration,
                    )
                    spec_file = reuse_deployment_spec_for_iteration(
                        iteration_path=iteration_path,
                        sample_dir=sample_dir,
                        source_iteration_id=spec_source_iteration,
                        target_iteration_id=iteration_id,
                        extra_labels=bench_labels_dict,
                        logger=logger,
                    )
                    update_iteration_meta(
                        iteration_path,
                        spec_regenerated=False,
                        spec_reused_from=spec_source_iteration,
                    )
                else:
                    from .cluster.capacity import collect_cluster_capacity

                    spec_file, gen_err = generate_and_write_spec(
                        task=task,
                        results_dir=results_dir,
                        sample=sample,
                        iteration_path=iteration_path,
                        iteration_id=iteration_id,
                        logger=logger,
                        capacity=collect_cluster_capacity(),
                        prior_feedback=prior_feedback,
                        max_validation_retries=1,
                        phase_index=phase_index,
                        total_phases=total_phases,
                        vllm_port=vllm_port,
                    )
                    if spec_file is None:
                        fail_iteration_phase(
                            iteration_path=iteration_path,
                            save_dir=save_dir,
                            sample=sample,
                            iteration_id=iteration_id,
                            failure_reason=gen_err or "static spec validation failed",
                            kind="spec",
                            logger=logger,
                        )
                        continue

                    probe = probe_iteration_deployable(
                        iteration_path=iteration_path,
                        image_id=image_id,
                        sample_slug=sample_slug,
                        app_port=task.env.port,
                        needs_db=task.scenario.needs_db,
                        wait_timeout_s=k8s_wait_timeout,
                        labels=bench_labels_dict,
                        logger=logger,
                    )
                    if not probe.ok:
                        fail_iteration_phase(
                            iteration_path=iteration_path,
                            save_dir=save_dir,
                            sample=sample,
                            iteration_id=iteration_id,
                            failure_reason=probe.reason,
                            kind="spec",
                            logger=logger,
                        )
                        continue
                    update_iteration_meta(iteration_path, spec_regenerated=True)

                spec_file = spec_file or find_iteration_spec_path(iteration_path)
                if spec_file is None:
                    fail_iteration_phase(
                        iteration_path=iteration_path,
                        save_dir=save_dir,
                        sample=sample,
                        iteration_id=iteration_id,
                        failure_reason="no spec.yaml after prepare",
                        kind=folder_kind,
                        logger=logger,
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
                    rebuild_code_dir=latest_code_dir(
                        task.get_sample_dir(results_dir, sample),
                        fallback=task.get_code_dir(results_dir, sample),
                    ),
                )

                try:
                    spec = K8sWorkloadSpec.from_yaml_file(spec_file)
                    fb = collect_iteration_feedback(
                        perf_run_dir=run_dir,
                        iteration_path=iteration_path,
                        namespace=spec.namespace,
                        logger=logger,
                    )
                    write_feedback_artifact(run_dir, fb)
                    prior_feedback = fb
                    update_iteration_meta(
                        iteration_path,
                        status="success",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
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
            from .llm_cost import refresh_k8s_cost_summary

            refresh_k8s_cost_summary(sample_dir)
        except Exception as exc:
            sample_logger.warning("Could not refresh LLM cost summary: %s", exc)

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

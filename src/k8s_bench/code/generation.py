"""
Single-attempt code generation helpers for baseline and refinement.

Retry loops and phase failure handling live in :mod:`k8s_bench.stages.code`.
Each attempt follows: LLM call → parse → snapshot code → functional tests.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from llm import Parser

from ..feedback import IterationFeedback
from ..failure import FunctionalFailureReport, build_functional_failure_report
from ..util.sample import append_k8s_skip
from ..workspace import (
    PROMPT_LOG_FILENAME,
    RESPONSE_LOG_FILENAME,
    attempt_subdir,
    ensure_iteration_core_layout,
    image_id_from_test_log,
    iteration_code_attempts_dir,
    iteration_code_phase_dir,
    iteration_code_snapshot_dir,
    iteration_functional_tests_dir,
    iteration_id_for_index,
    mark_iteration_folder_failed,
    next_attempt_index,
    parse_iteration_folder_name,
)
from .baseline_meta import (
    append_baseline_summary,
    capture_baseline_retry_state,
    reset_baseline_phase_on_force,
    rotate_top_level_into_attempt,
    write_attempt_meta,
    write_codegen_meta,
)
from .prompts import (
    build_baseline_prompt,
    build_code_refinement_prompt,
    call_llm_with_retries,
)
from .shared import (
    classify_ft_failure,
    ft_pass_counts,
    run_functional_tests,
    write_code_files,
)

CodegenMode = Literal["baseline", "refinement"]


@dataclass
class CodegenRetryState:
    """Mutable feedback carried across attempts within one codegen stage."""

    last_attempt_code: str = ""
    last_failure_report: FunctionalFailureReport | None = None
    same_iteration_report: FunctionalFailureReport | None = None


@dataclass(frozen=True)
class CodegenAttemptResult:
    """Outcome of one codegen attempt (LLM + parse + FT)."""

    passed: bool = False
    image_id: str | None = None
    code_dir: Path | None = None
    error: str | None = None
    infra_failure: bool = False
    continue_loop: bool = True


def prepare_codegen_workspace(
    *,
    mode: CodegenMode,
    iteration_path: Path,
    force: bool,
) -> Path:
    """Ensure ``02-code/`` layout exists; reset baseline artifacts when ``force``."""
    ensure_iteration_core_layout(iteration_path)
    phase_dir = iteration_code_phase_dir(iteration_path)
    phase_dir.mkdir(parents=True, exist_ok=True)
    if mode == "baseline" and force:
        reset_baseline_phase_on_force(iteration_path)
    return phase_dir


def generate_and_validate_code(
    *,
    mode: CodegenMode,
    attempt_index: int,
    max_attempts: int,
    task: Any,
    results_dir: Path,
    sample: int,
    sample_dir: Path,
    iteration_path: Path,
    phase_dir: Path,
    logger: logging.Logger,
    ft_timeout: int,
    num_ports: int,
    min_port: int,
    vllm_port: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    prior_feedback: IterationFeedback | None,
    prior_failure_report: FunctionalFailureReport | None,
    retry_state: CodegenRetryState,
    iteration_index: int,
    total_iterations: int,
    track_attempt_dirs: bool,
    fail_fast_on_infra: bool,
    experiment_id: str = "default",
    llm_max_cost_usd: float | None = None,
) -> CodegenAttemptResult:
    """
    Run one codegen attempt: LLM → parse → write code → functional tests.

    Updates ``retry_state`` when another attempt may use prior failure context.
    """
    llm_call_type = (
        "baseline_code_generation" if mode == "baseline" else "code_refinement"
    )
    log_label = "Baseline codegen" if mode == "baseline" else "Code refinement"
    started_at = time.time()

    from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

    try:
        check_k8s_llm_budget(
            sample_dir,
            experiment_id=experiment_id,
            max_cost_usd=llm_max_cost_usd,
        )
    except Exception as exc:
        error = f"LLM budget exceeded: {exc}"
        logger.error(error)
        return CodegenAttemptResult(error=error, continue_loop=False)

    try:
        prompt_text, raw_response, prompter = _llm_call_for_mode(
            mode=mode,
            task=task,
            results_dir=results_dir,
            sample=sample,
            sample_dir=sample_dir,
            iteration_path=iteration_path,
            logger=logger,
            vllm_port=vllm_port,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            prior_feedback=prior_feedback,
            prior_failure_report=prior_failure_report,
            same_iteration_failure_report=retry_state.same_iteration_report,
            last_attempt_code=retry_state.last_attempt_code,
            last_failure_report=retry_state.last_failure_report,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            log_label=log_label,
            experiment_id=experiment_id,
        )
    except Exception as exc:
        error = f"LLM call failed: {exc}"
        if track_attempt_dirs:
            attempt_dir = attempt_subdir(
                iteration_code_attempts_dir(iteration_path), attempt_index
            )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            write_attempt_meta(
                attempt_dir,
                attempt_index=attempt_index,
                status="llm_failed",
                error=error,
                num_passed_ft=None,
                num_total_ft=None,
                duration_s=time.time() - started_at,
            )
            return CodegenAttemptResult(error=error, continue_loop=True)
        return CodegenAttemptResult(error=error, continue_loop=False)

    (phase_dir / PROMPT_LOG_FILENAME).write_text(prompt_text + "\n", encoding="utf-8")
    (phase_dir / RESPONSE_LOG_FILENAME).write_text(
        raw_response + "\n", encoding="utf-8"
    )

    iter_id = _iteration_id_for_llm_cost(iteration_path, iteration_index)
    record_k8s_llm_call(
        prompter=prompter,
        call_type=llm_call_type,
        sample_dir=sample_dir,
        logger=logger,
        artifact_dir=phase_dir,
        iteration_id=iter_id,
        note=f"attempt={attempt_index}",
        experiment_id=experiment_id,
        max_cost_usd=llm_max_cost_usd,
    )

    files = Parser(task.env, logger).parse_response(raw_response)
    if Path("failed") in files:
        error = "parse failure (LLM response did not contain expected code blocks)"
        logger.warning(error)
        if track_attempt_dirs:
            attempt_dir = attempt_subdir(
                iteration_code_attempts_dir(iteration_path), attempt_index
            )
            rotate_top_level_into_attempt(iteration_path, attempt_dir)
            write_attempt_meta(
                attempt_dir,
                attempt_index=attempt_index,
                status="parse_failed",
                error=error,
                num_passed_ft=None,
                num_total_ft=None,
                duration_s=time.time() - started_at,
            )
            return CodegenAttemptResult(error=error, continue_loop=True)
        return CodegenAttemptResult(error=error, continue_loop=False)

    code_dir = iteration_code_snapshot_dir(iteration_path)
    write_code_files(files, code_dir)
    logger.info("%s attempt %d: wrote %s", log_label, attempt_index, code_dir)

    ft_dir = iteration_functional_tests_dir(iteration_path)
    try:
        passed = run_functional_tests(
            task,
            code_dir=code_dir,
            ft_dir=ft_dir,
            ft_timeout=ft_timeout,
            num_ports=num_ports,
            min_port=min_port,
        )
    except Exception as exc:
        is_infra = False
        hint = ""
        excerpt = ""
        if ft_dir.is_dir():
            is_infra, hint, excerpt = classify_ft_failure(ft_dir)
        detailed = f"functional-test runner crashed: {exc!r}"
        error = f"{detailed} — infra failure: {hint}" if is_infra else detailed
        if is_infra:
            logger.error(
                "%s attempt %d/%d: infra failure during FT harness (%s)",
                log_label,
                attempt_index,
                max_attempts,
                hint,
            )
        else:
            logger.exception(detailed, exc_info=exc)

        if track_attempt_dirs:
            attempt_dir = attempt_subdir(
                iteration_code_attempts_dir(iteration_path), attempt_index
            )
            rotate_top_level_into_attempt(iteration_path, attempt_dir)
            write_attempt_meta(
                attempt_dir,
                attempt_index=attempt_index,
                status="infra_failed" if is_infra else "ft_runner_failed",
                error=error,
                num_passed_ft=None,
                num_total_ft=None,
                duration_s=time.time() - started_at,
                infra_failure=is_infra,
                error_excerpt=excerpt or None,
            )
            stop = is_infra and fail_fast_on_infra
            return CodegenAttemptResult(
                error=error,
                infra_failure=is_infra,
                continue_loop=not stop,
            )
        return CodegenAttemptResult(
            error=error,
            infra_failure=is_infra,
            continue_loop=False,
        )

    num_passed, num_total = ft_pass_counts(ft_dir)
    if passed and num_total > 0 and num_passed >= num_total:
        image_id = image_id_from_test_log(ft_dir / "test.log")
        if image_id is None:
            sample_logger = logging.getLogger(task.id)
            image_id = task._build_image_from_code_dir(code_dir, sample_logger)
        if image_id is None:
            error = "FTs passed but could not resolve a docker image id"
            logger.error(error)
            return CodegenAttemptResult(error=error, continue_loop=False)

        logger.info(
            "%s succeeded on attempt %d (image=%s, FT=%d/%d passing)",
            log_label,
            attempt_index,
            image_id,
            num_passed,
            num_total,
        )
        return CodegenAttemptResult(
            passed=True,
            image_id=image_id,
            code_dir=code_dir,
            continue_loop=False,
        )

    is_infra, hint, excerpt = classify_ft_failure(ft_dir)
    base_error = f"functional tests failed ({num_passed}/{num_total} passing)"
    error = f"{base_error} — infra failure: {hint}" if is_infra else base_error
    if is_infra:
        logger.error(
            "%s attempt %d/%d: infra failure (%s)",
            log_label,
            attempt_index,
            max_attempts,
            hint,
        )
    else:
        logger.warning(
            "%s attempt %d/%d failed: %s",
            log_label,
            attempt_index,
            max_attempts,
            error,
        )

    if mode == "baseline" and not is_infra:
        (
            retry_state.last_attempt_code,
            retry_state.last_failure_report,
        ) = capture_baseline_retry_state(
            iteration_path=iteration_path,
            code_dir=code_dir,
            iteration_id=iteration_id_for_index(0),
            logger=logger,
        )
    elif mode == "refinement":
        retry_state.same_iteration_report = build_functional_failure_report(
            iteration_path, logger=logger
        )
        logger.warning(
            "functional tests failed after %s attempt %d/%d (%d/%d FT passed)",
            log_label.lower(),
            attempt_index,
            max_attempts,
            retry_state.same_iteration_report.num_passed_ft,
            retry_state.same_iteration_report.num_total_ft,
        )

    if track_attempt_dirs:
        attempt_dir = attempt_subdir(
            iteration_code_attempts_dir(iteration_path), attempt_index
        )
        rotate_top_level_into_attempt(iteration_path, attempt_dir)
        write_attempt_meta(
            attempt_dir,
            attempt_index=attempt_index,
            status="infra_failed" if is_infra else "ft_failed",
            error=error,
            num_passed_ft=num_passed,
            num_total_ft=num_total,
            duration_s=time.time() - started_at,
            infra_failure=is_infra,
            error_excerpt=excerpt or None,
        )
        stop = is_infra and fail_fast_on_infra
        return CodegenAttemptResult(
            error=error,
            infra_failure=is_infra,
            continue_loop=not stop,
        )

    return CodegenAttemptResult(error=error, infra_failure=is_infra, continue_loop=True)


def record_baseline_codegen_success(
    *,
    iteration_path: Path,
    sample_dir: Path,
    task: Any,
    attempts_used: int,
    max_attempts: int,
    winning_attempt: int,
    logger: logging.Logger,
) -> None:
    write_codegen_meta(
        iteration_path,
        status="passed",
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        task=task,
        winning_attempt=winning_attempt,
    )
    append_baseline_summary(
        sample_dir=sample_dir,
        iteration_path=iteration_path,
        task=task,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        winning_attempt=winning_attempt,
        status="passed",
        error=None,
        logger=logger,
    )


def finalize_baseline_codegen_failure(
    *,
    task: Any,
    sample: int,
    sample_dir: Path,
    task_run_dir: Path | None,
    iteration_path: Path,
    max_attempts: int,
    last_error: str | None,
    terminal_infra_failure: bool,
    logger: logging.Logger,
) -> None:
    """Persist baseline terminal failure metadata and mark the folder failed."""
    attempts_used = next_attempt_index(iteration_code_attempts_dir(iteration_path)) - 1
    terminal_status = "infra_failed" if terminal_infra_failure else "failed"
    write_codegen_meta(
        iteration_path,
        status=terminal_status,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        task=task,
        winning_attempt=None,
        error=last_error,
        infra_failure=terminal_infra_failure,
    )
    append_baseline_summary(
        sample_dir=sample_dir,
        iteration_path=iteration_path,
        task=task,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        winning_attempt=None,
        status=terminal_status,
        error=last_error,
        logger=logger,
    )

    if task_run_dir is not None:
        skip_reason = (
            f"skipped: baseline codegen aborted on infra failure after "
            f"{attempts_used} attempt(s) — fix the host environment and rerun "
            f"(last error: {last_error or 'unknown'})"
            if terminal_infra_failure
            else (
                f"skipped: baseline codegen failed after {max_attempts} attempt(s) "
                f"(last error: {last_error or 'unknown'})"
            )
        )
        append_k8s_skip(task_run_dir, sample, skip_reason)

    try:
        mark_iteration_folder_failed(iteration_path)
    except FileExistsError:
        pass


def _llm_call_for_mode(
    *,
    mode: CodegenMode,
    task: Any,
    results_dir: Path,
    sample: int,
    sample_dir: Path,
    iteration_path: Path,
    logger: logging.Logger,
    vllm_port: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    prior_feedback: IterationFeedback | None,
    prior_failure_report: FunctionalFailureReport | None,
    same_iteration_failure_report: FunctionalFailureReport | None,
    last_attempt_code: str,
    last_failure_report: FunctionalFailureReport | None,
    iteration_index: int,
    total_iterations: int,
    log_label: str,
    experiment_id: str = "default",
) -> tuple[str, str, Any]:
    from ..session import get_experiment_session

    prompter = get_experiment_session(
        task,
        sample_dir,
        sample,
        vllm_port=vllm_port,
        experiment_id=experiment_id,
        logger=logger,
    )

    if mode == "baseline":
        prompt_text = build_baseline_prompt(
            prompter,
            prior_code=last_attempt_code,
            failure_report=last_failure_report,
        )
    else:
        if prior_feedback is None:
            raise ValueError("refinement codegen requires prior_feedback")
        prompt_text = build_code_refinement_prompt(
            task=task,
            results_dir=results_dir,
            sample=sample,
            iteration_path=iteration_path,
            prior_feedback=prior_feedback,
            same_iteration_failure_report=same_iteration_failure_report,
            prior_failure_report=prior_failure_report,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
        )

    raw_response = call_llm_with_retries(
        prompter=prompter,
        prompt_text=prompt_text,
        sample_dir=sample_dir,
        logger=logger,
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        log_label=log_label,
    )
    return prompt_text, raw_response, prompter


def _iteration_id_for_llm_cost(iteration_path: Path, iteration_index: int) -> str:
    idx, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
    if idx is not None:
        return iteration_id_for_index(idx)
    return iteration_id_for_index(iteration_index)

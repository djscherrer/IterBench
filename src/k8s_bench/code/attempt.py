"""
Single code-generation attempt: LLM call, parse, snapshot, functional tests.

Retry loops and phase failure handling live in :mod:`k8s_bench.stages.code`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from llm import Prompter

from ..failure import (
    CodeFailureRecord,
    build_code_failure_record,
    classify_ft_failure,
    code_attempt_dir,
    docker_build_failed_in_test_log,
    load_prior_code_attempt_failure,
    write_attempt_failure,
)
from ..session import persist_session
from workspace import (
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
    parse_iteration_folder_name,
)
from .baseline_meta import (
    reset_baseline_phase_on_force,
    rotate_top_level_into_attempt,
    write_attempt_meta,
)
from .parse import parse_code_response
from .prompts import build_baseline_prompt, build_code_refinement_prompt
from .shared import (
    ft_pass_counts,
    read_log_tail,
    run_functional_tests,
    write_code_files,
)

CodegenMode = Literal["baseline", "refinement"]


@dataclass(frozen=True)
class CodegenAttemptResult:
    """Outcome of one codegen attempt (LLM + parse + FT)."""

    passed: bool = False
    image_id: str | None = None
    code_dir: Path | None = None
    error: str | None = None
    infra_failure: bool = False
    continue_loop: bool = True
    failure: CodeFailureRecord | None = None


def _attempt_dir(
    iteration_path: Path, *, attempt_index: int, enable_attempts: bool
) -> Path | None:
    """
    Resolve the attempt directory used for baseline retries.

    When ``enable_attempts`` is false, we don't persist
    per-attempt artifacts and return ``None``.
    """
    if not enable_attempts:
        return None
    return attempt_subdir(iteration_code_attempts_dir(iteration_path), attempt_index)


def _record_attempt_meta(
    *,
    iteration_path: Path,
    attempt_index: int,
    enable_attempts: bool,
    status: str,
    started_at: float,
    error: str,
    infra_failure: bool = False,
    num_passed_ft: int | None = None,
    num_total_ft: int | None = None,
    error_excerpt: str | None = None,
    rotate_top_level: bool = False,
) -> Path | None:
    """
    Best-effort write of attempt metadata (baseline only).

    ``rotate_top_level`` is used once we have written a "top level" snapshot
    (e.g. code + functional_tests) and want to preserve it under
    ``attempts/<NNN>/`` before the next retry overwrites it.
    """
    attempt_dir = _attempt_dir(
        iteration_path, attempt_index=attempt_index, enable_attempts=enable_attempts
    )
    if attempt_dir is None:
        return None
    if rotate_top_level:
        rotate_top_level_into_attempt(iteration_path, attempt_dir)
    else:
        attempt_dir.mkdir(parents=True, exist_ok=True)
    write_attempt_meta(
        attempt_dir,
        attempt_index=attempt_index,
        status=status,
        error=error,
        num_passed_ft=num_passed_ft,
        num_total_ft=num_total_ft,
        duration_s=time.time() - started_at,
        infra_failure=infra_failure,
        error_excerpt=error_excerpt,
    )
    return attempt_dir


def _persist_code_attempt_failure(
    *,
    iteration_path: Path,
    attempt_index: int,
    iteration_id: str,
    enable_attempts: bool,
    record: CodeFailureRecord,
    logger: logging.Logger,
) -> None:
    """Write ``attempts/NNN/failure.json`` (always) and rotate baseline snapshots."""
    attempt_dir = _attempt_dir(
        iteration_path, attempt_index=attempt_index, enable_attempts=enable_attempts
    )
    if attempt_dir is None:
        attempt_dir = code_attempt_dir(iteration_path, attempt_index)
    try:
        write_attempt_failure(attempt_dir, record)
    except Exception as exc:
        logger.warning(
            "could not persist attempt failure for %s attempt %d: %s",
            iteration_id,
            attempt_index,
            exc,
        )


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


def run_code_attempt(
    *,
    mode: CodegenMode,
    attempt_index: int,
    max_attempts: int,
    prompter: Prompter,
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
    max_retries: int,
    base_delay: float,
    max_delay: float,
    prior_iteration_failure: CodeFailureRecord | None,
    iteration_index: int,
    total_iterations: int,
    enable_attempts: bool,
    fail_fast_on_infra: bool,
    experiment_id: str = "default",
    llm_max_cost_usd: float | None = None,
) -> CodegenAttemptResult:
    """
    Run one codegen attempt: LLM → parse → write code → functional tests.

  Attempt failures are persisted under ``02-code/attempts/NNN/failure.json``.
    """
    llm_call_type = (
        "baseline_code_generation" if mode == "baseline" else "code_refinement"
    )
    log_label = "Baseline codegen" if mode == "baseline" else "Code refinement"
    started_at = time.time()

    from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

    # ---------------------------------------------------------------------
    # Phase 0: global guardrails (LLM budget)
    # ---------------------------------------------------------------------
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

    iteration_id = _iteration_id_for_llm_cost(iteration_path, iteration_index)
    prior_attempt_failure = (
        load_prior_code_attempt_failure(iteration_path, attempt_index)
        if enable_attempts
        else None
    )

    # ---------------------------------------------------------------------
    # Phase 1: build prompt + call LLM
    # ---------------------------------------------------------------------
    try:
        if mode == "baseline":
            prompt_text = build_baseline_prompt(
                task=task,
                iteration_id=iteration_id,
                iteration_index=iteration_index,
                total_iterations=total_iterations,
                prior_attempt_failure=prior_attempt_failure,
            )
        else:
            prompt_text = build_code_refinement_prompt(
                task=task,
                results_dir=results_dir,
                sample=sample,
                iteration_id=iteration_id,
                prior_attempt_failure=prior_attempt_failure,
                prior_iteration_failure=prior_iteration_failure,
                iteration_index=iteration_index,
                total_iterations=total_iterations,
                experiment_id=experiment_id,
            )
        raw_response = prompter.send_with_retries(
            prompt_text,
            logger,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            log_label=log_label,
        )
        persist_session(
            prompter, sample_dir, experiment_id=experiment_id, logger=logger
        )
    except Exception as exc:
        error = f"LLM call failed: {exc}"
        llm_record = CodeFailureRecord(
            phase="code",
            kind="llm_call",
            iteration_id=iteration_id,
            attempt=attempt_index,
            summary=error,
            llm_error=error,
        )
        _persist_code_attempt_failure(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            iteration_id=iteration_id,
            enable_attempts=enable_attempts,
            record=llm_record,
            logger=logger,
        )
        _record_attempt_meta(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            status="llm_failed",
            started_at=started_at,
            error=error,
        )
        return CodegenAttemptResult(error=error, continue_loop=True, failure=llm_record)

    # Persist raw prompt/response for debugging and auditability.
    (phase_dir / PROMPT_LOG_FILENAME).write_text(prompt_text + "\n", encoding="utf-8")
    (phase_dir / RESPONSE_LOG_FILENAME).write_text(
        raw_response + "\n", encoding="utf-8"
    )

    # ---------------------------------------------------------------------
    # Phase 1b: record LLM cost accounting (after we have the raw response)
    # ---------------------------------------------------------------------
    record_k8s_llm_call(
        prompter=prompter,
        call_type=llm_call_type,
        sample_dir=sample_dir,
        logger=logger,
        artifact_dir=phase_dir,
        iteration_id=iteration_id,
        note=f"attempt={attempt_index}",
        experiment_id=experiment_id,
        max_cost_usd=llm_max_cost_usd,
    )

    # ---------------------------------------------------------------------
    # Phase 2: parse LLM output into files
    # ---------------------------------------------------------------------
    try:
        files = parse_code_response(
            raw_response, env=task.env, logger=logger
        )
    except ValueError as exc:
        error = str(exc)
        logger.warning(error)
        parse_record = CodeFailureRecord(
            phase="code",
            kind="llm_parse",
            iteration_id=iteration_id,
            attempt=attempt_index,
            summary=error,
            llm_error=error,
        )
        _persist_code_attempt_failure(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            iteration_id=iteration_id,
            enable_attempts=enable_attempts,
            record=parse_record,
            logger=logger,
        )
        _record_attempt_meta(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            status="parse_failed",
            started_at=started_at,
            error=error,
            rotate_top_level=True,
        )
        return CodegenAttemptResult(error=error, continue_loop=True, failure=parse_record)

    # ---------------------------------------------------------------------
    # Phase 3: write code snapshot to disk (this becomes the "current attempt")
    # ---------------------------------------------------------------------
    code_dir = iteration_code_snapshot_dir(iteration_path)
    write_code_files(files, code_dir)
    logger.info("%s attempt %d: wrote %s", log_label, attempt_index, code_dir)

    # ---------------------------------------------------------------------
    # Phase 4: run functional tests (includes docker build + container startup)
    # ---------------------------------------------------------------------
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
        # This is the *functional-test runner* crashing (Python exception), not a
        # test asserting a failure. We classify infra failures using the test.log
        # we managed to write (if any).
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

        runner_record = build_code_failure_record(
            iteration_path,
            iteration_id=iteration_id,
            attempt=attempt_index,
            logger=logger,
        )
        if not is_infra:
            runner_record = CodeFailureRecord(
                phase="code",
                kind="ft_runner",
                iteration_id=iteration_id,
                attempt=attempt_index,
                summary=error,
                llm_error=error,
            )
        _persist_code_attempt_failure(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            iteration_id=iteration_id,
            enable_attempts=enable_attempts,
            record=runner_record,
            logger=logger,
        )
        _record_attempt_meta(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            status="infra_failed" if is_infra else "ft_runner_failed",
            started_at=started_at,
            error=error,
            infra_failure=is_infra,
            error_excerpt=excerpt or None,
            rotate_top_level=True,
        )
        stop = is_infra and fail_fast_on_infra
        return CodegenAttemptResult(
            error=error,
            infra_failure=is_infra,
            continue_loop=not stop,
            failure=runner_record,
        )

    # ---------------------------------------------------------------------
    # Phase 4b: interpret FT results and possibly resolve docker image id
    # ---------------------------------------------------------------------
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

    # ---------------------------------------------------------------------
    # Phase 5: failure classification + persistence
    # ---------------------------------------------------------------------
    is_infra, hint, excerpt = classify_ft_failure(ft_dir)
    test_log_text = read_log_tail(ft_dir / "test.log")
    if docker_build_failed_in_test_log(test_log_text):
        base_error = "docker image build failed (functional tests were not run)"
    else:
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

    failure_record = build_code_failure_record(
        iteration_path,
        iteration_id=iteration_id,
        attempt=attempt_index,
        logger=logger,
    )
    _persist_code_attempt_failure(
        iteration_path=iteration_path,
        attempt_index=attempt_index,
        iteration_id=iteration_id,
        enable_attempts=enable_attempts,
        record=failure_record,
        logger=logger,
    )
    logger.warning(
        "functional tests failed after %s attempt %d/%d (%d/%d FT passed)",
        log_label.lower(),
        attempt_index,
        max_attempts,
        failure_record.num_passed_ft,
        failure_record.num_total_ft,
    )

    _record_attempt_meta(
        iteration_path=iteration_path,
        attempt_index=attempt_index,
        enable_attempts=enable_attempts,
        status="infra_failed" if is_infra else "ft_failed",
        started_at=started_at,
        error=error,
        infra_failure=is_infra,
        num_passed_ft=num_passed,
        num_total_ft=num_total,
        error_excerpt=excerpt or None,
        rotate_top_level=True,
    )
    stop = is_infra and fail_fast_on_infra
    return CodegenAttemptResult(
        error=error,
        infra_failure=is_infra,
        continue_loop=not stop,
        failure=failure_record,
    )


def _iteration_id_for_llm_cost(iteration_path: Path, iteration_index: int) -> str:
    idx, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
    if idx is not None:
        return iteration_id_for_index(idx)
    return iteration_id_for_index(iteration_index)

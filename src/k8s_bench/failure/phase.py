"""Mark failed k8s iteration phases and update experiment metadata."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from workspace.meta import update_iteration_meta
from workspace.paths import mark_iteration_folder_failed
from workspace.skips import append_k8s_skip
from .persist import write_iteration_failure
from .record import (
    BenchFailureRecord,
    CodeFailureRecord,
    DecisionFailureRecord,
    DeployFailureRecord,
    IterationFailure,
    Phase,
    SpecFailureRecord,
)


def fail_iteration_phase(
    *,
    iteration_path: Path,
    task_run_dir: Path,
    sample_dir: Path,
    sample: int,
    iteration_id: str,
    kind: str,
    logger: logging.Logger,
    failure_reason: str = "",
    iteration_failure: IterationFailure | None = None,
) -> Path:
    """
    Mark an iteration as failed: persist ``failure.json``, update meta, rename
    folder with ``-failed`` suffix, and append a failure block to the summary.
    """
    phase: Phase = (
        kind if kind in {"decision", "code", "spec", "deploy", "bench"} else "code"
    )  # type: ignore[assignment]
    if iteration_failure is None:
        summary = failure_reason or f"{phase} phase failed"
        if phase == "decision":
            terminal = DecisionFailureRecord(
                phase="decision",
                kind="llm_call",
                iteration_id=iteration_id,
                summary=summary,
                llm_error=summary,
            )
        elif phase == "spec":
            terminal = SpecFailureRecord(
                phase="spec",
                kind="spec_validation",
                iteration_id=iteration_id,
                summary=summary,
            )
        elif phase == "deploy":
            terminal = DeployFailureRecord(
                phase="deploy",
                kind="unknown",
                iteration_id=iteration_id,
                summary=summary,
            )
        elif phase == "bench":
            terminal = BenchFailureRecord(
                phase="bench",
                kind="unknown",
                iteration_id=iteration_id,
                summary=summary,
            )
        else:
            terminal = CodeFailureRecord(
                phase="code",
                kind="functional_test",
                iteration_id=iteration_id,
                summary=summary,
            )
        iteration_failure = IterationFailure(
            iteration_id=iteration_id,
            phase=phase,
            terminal=terminal,
        )

    update_iteration_meta(
        iteration_path,
        status="failed",
        failure_reason=iteration_failure.terminal.summary,
        failure_kind=phase,
        refinement_action=phase if phase in {"code", "spec"} else None,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        write_iteration_failure(iteration_path, iteration_failure)
        logger.info(
            "wrote iteration failure for %s (%s/%s, %d attempt record(s))",
            iteration_id,
            phase,
            iteration_failure.terminal.kind,
            len(iteration_failure.attempts),
        )
    except Exception as exc:
        logger.warning(
            "Could not persist iteration failure for %s: %s",
            iteration_id,
            exc,
        )

    try:
        failed_path = mark_iteration_folder_failed(iteration_path)
    except FileExistsError as exc:
        logger.error("Could not rename failed iteration folder: %s", exc)
        failed_path = iteration_path

    try:
        from ..experiment_summary import append_iteration_failure_block
        from ..feedback import read_failed_iteration_error_excerpt

        excerpt = read_failed_iteration_error_excerpt(failed_path)
        append_iteration_failure_block(
            sample_dir=sample_dir,
            iteration_id=iteration_id,
            iteration_path=failed_path,
            failure_reason=iteration_failure.terminal.summary,
            kind=phase,
            error_excerpt=excerpt,
            iteration_failure=iteration_failure,
        )
    except Exception as exc:
        logger.warning(
            "Could not append failure block to experiment summary for %s: %s",
            iteration_id,
            exc,
        )

    append_k8s_skip(
        task_run_dir,
        sample,
        f"failed phase {iteration_id}: {iteration_failure.terminal.summary}",
    )
    logger.warning(
        "phase %s failed (%s): %s → %s",
        iteration_id,
        phase,
        iteration_failure.terminal.summary,
        failed_path.name,
    )
    return failed_path

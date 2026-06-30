"""Mark failed k8s iteration phases and update experiment metadata."""

from __future__ import annotations

import logging
from pathlib import Path

from ..workspace.skips import append_k8s_skip
from ..workspace.meta import update_iteration_meta
from ..workspace.paths import mark_iteration_folder_failed
from .build import build_functional_failure_report


def fail_iteration_phase(
    *,
    iteration_path: Path,
    task_run_dir: Path,
    sample_dir: Path,
    sample: int,
    iteration_id: str,
    failure_reason: str,
    kind: str,
    logger: logging.Logger,
) -> Path:
    """
    Mark an iteration as failed: update meta, rename folder with ``-failed`` suffix,
    and append a failure block to the experiment summary.

    For ``kind="code"`` we also build and persist ``failure_report.json`` so the
    next refinement iteration receives structured FT diagnostics.

    Returns the renamed iteration directory.
    """
    update_iteration_meta(
        iteration_path,
        status="failed",
        failure_reason=failure_reason,
        failure_kind=kind,
        refinement_action=kind if kind in {"code", "spec"} else None,
    )

    if kind == "code":
        try:
            from ..workspace import write_failure_report

            report = build_functional_failure_report(
                iteration_path,
                iteration_id=iteration_id,
                logger=logger,
            )
            write_failure_report(iteration_path, report)
            logger.info(
                "wrote functional failure report for %s: %d/%d passed, failed=%s",
                iteration_id,
                report.num_passed_ft,
                report.num_total_ft,
                [ft.name for ft in report.failed_tests] or "(unknown)",
            )
        except Exception as exc:
            logger.warning(
                "Could not persist functional failure report for %s: %s",
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
            failure_reason=failure_reason,
            kind=kind,
            error_excerpt=excerpt,
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
        f"failed phase {iteration_id}: {failure_reason}",
    )
    logger.warning(
        "phase %s failed (%s): %s → %s",
        iteration_id,
        kind,
        failure_reason,
        failed_path.name,
    )
    return failed_path

"""Mark failed k8s iteration phases and update experiment metadata."""

from __future__ import annotations

import logging
from pathlib import Path

from .util.sample import append_k8s_skip
from .workspace.meta import update_iteration_meta
from .workspace.paths import mark_iteration_folder_failed


def fail_iteration_phase(
    *,
    iteration_path: Path,
    save_dir: Path,
    sample: int,
    iteration_id: str,
    failure_reason: str,
    kind: str,
    logger: logging.Logger,
) -> Path:
    """
    Mark an iteration as failed: update meta and rename folder with ``-failed`` suffix.

    Returns the renamed iteration directory.
    """
    update_iteration_meta(
        iteration_path,
        status="failed",
        failure_reason=failure_reason,
        refinement_action=kind if kind in {"code", "spec", "baseline"} else None,
    )
    try:
        failed_path = mark_iteration_folder_failed(iteration_path)
    except FileExistsError as exc:
        logger.error("Could not rename failed iteration folder: %s", exc)
        failed_path = iteration_path

    append_k8s_skip(
        save_dir,
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


def record_pending_code_refinement_after_failure(
    sample_dir: Path,
    *,
    failed_iteration_id: str,
    reason: str,
) -> None:
    from .refinement.code import record_pending_code_refinement

    record_pending_code_refinement(
        sample_dir,
        failed_iteration_id=failed_iteration_id,
        reason=reason,
    )

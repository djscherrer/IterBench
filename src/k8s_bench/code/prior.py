"""Load prior code-refinement failure reports from disk."""

from __future__ import annotations

from pathlib import Path

from ..failure import FunctionalFailureReport
from ..workspace import (
    iteration_folder_is_failed,
    iterations_root,
    load_failure_report,
    parse_iteration_index,
)


def find_latest_prior_failure_report(
    sample_dir: Path,
    *,
    current_iteration_index: int,
    experiment_id: str | None = None,
) -> FunctionalFailureReport | None:
    root = iterations_root(sample_dir, experiment_id=experiment_id)
    if not root.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if not iteration_folder_is_failed(child.name):
            continue
        if "-code" not in child.name:
            continue
        idx = parse_iteration_index(child.name)
        if idx is None or idx >= current_iteration_index:
            continue
        if best is None or idx > best[0]:
            best = (idx, child)
    if best is None:
        return None
    return load_failure_report(best[1])

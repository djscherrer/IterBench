"""Resolve which application code directory is active for k8s experiments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workspace import (
    iteration_code_snapshot_dir,
    iteration_folder_is_failed,
    iterations_root,
    parse_iteration_phase,
)


def resolve_active_code_dir(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    sample_dir: Path | None = None,
) -> Path:
    """
    Return the newest iteration ``code/`` snapshot with content, else ``sampleN/code/``.

    Sample ``code/`` is the immutable baseline; refined code lives under
    ``iterations/iteration-NNN-code/code/`` only.
    """
    if sample_dir is None:
        sample_dir = task.get_sample_dir(results_dir, sample)

    root = iterations_root(sample_dir)
    best: tuple[int, Path] | None = None
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or iteration_folder_is_failed(child.name):
                continue
            code_dir = iteration_code_snapshot_dir(child)
            if not code_dir.is_dir():
                continue
            if not any(code_dir.iterdir()):
                continue
            phase = parse_iteration_phase(child.name)
            if phase is None:
                continue
            if best is None or phase > best[0]:
                best = (phase, code_dir)

    if best is not None:
        return best[1]
    return task.get_code_dir(results_dir, sample)


def resolve_image_id_from_ft_log(test_log: Path) -> str | None:
    import re

    pattern = re.compile(r"sha256:[0-9a-f]{64}")
    try:
        for line in test_log.read_text(encoding="utf-8").splitlines():
            match = pattern.search(line)
            if match:
                return match.group(0)
    except OSError:
        pass
    return None

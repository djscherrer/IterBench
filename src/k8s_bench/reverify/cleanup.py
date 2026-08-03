"""
Safe removal of stale generated benchmark/deploy artifacts before a re-bench.

Mirrors the cleanup ``scripts/k8s_run_iteration.sh`` performs before invoking
``--deploy-only --force`` for a single iteration folder: only ``04-deploy/``,
``05-bench/``, and the top-level ``iteration.log`` are removed. Everything
else — ``01-decision/``, ``02-code/``, ``03-spec/``, functional-test
artifacts, lineage metadata, LLM prompt/response logs — is left untouched.

Every delete goes through :func:`safe_rmtree` / :func:`safe_unlink`, which
refuse to act on any path that is not the results root itself or a
descendant of it. This is defense in depth: discovery only ever walks paths
under the results root, so a target outside it should never occur in
practice, but a single guard here means a bug anywhere upstream cannot turn
into a delete outside the copied results tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from workspace import iteration_bench_dir, iteration_deploy_dir, iteration_log_path


class UnsafeDeletionError(RuntimeError):
    """Raised when a delete target would fall outside the results root."""


def _assert_within_root(target: Path, root: Path) -> Path:
    resolved_target = target.resolve()
    resolved_root = root.resolve()
    is_root_itself = resolved_target == resolved_root
    is_descendant = resolved_root in resolved_target.parents
    if not (is_root_itself or is_descendant):
        raise UnsafeDeletionError(
            f"refusing to delete {resolved_target} — outside results root {resolved_root}"
        )
    return resolved_target


def safe_rmtree(target: Path, *, root: Path) -> None:
    resolved = _assert_within_root(target, root)
    if resolved.is_dir():
        shutil.rmtree(resolved)


def safe_unlink(target: Path, *, root: Path) -> None:
    resolved = _assert_within_root(target, root)
    if resolved.is_file():
        resolved.unlink()


def stale_artifact_targets(iteration_path: Path) -> list[Path]:
    """Paths cleared before a forced re-bench: ``04-deploy/``, ``05-bench/``, ``iteration.log``."""
    return [
        iteration_deploy_dir(iteration_path),
        iteration_bench_dir(iteration_path),
        iteration_log_path(iteration_path),
    ]


def clear_stale_reverify_artifacts(iteration_path: Path, *, root: Path) -> list[Path]:
    """Remove stale 04-deploy/05-bench/iteration.log; return the paths actually removed."""
    removed: list[Path] = []
    for target in stale_artifact_targets(iteration_path):
        if target.is_dir():
            safe_rmtree(target, root=root)
            removed.append(target)
        elif target.is_file():
            safe_unlink(target, root=root)
            removed.append(target)
    return removed


__all__ = [
    "UnsafeDeletionError",
    "safe_rmtree",
    "safe_unlink",
    "stale_artifact_targets",
    "clear_stale_reverify_artifacts",
]

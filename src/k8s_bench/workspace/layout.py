"""Create and maintain per-iteration workspace directories."""

from __future__ import annotations

import shutil
from pathlib import Path

from .paths import (
    PHASE_BENCH_DIRNAME,
    PHASE_DEPLOY_DIRNAME,
    PHASE_SPEC_DIRNAME,
    find_iteration_spec_path,
    iteration_bench_dir,
    iteration_code_snapshot_dir,
    iteration_functional_tests_dir,
)


def ensure_iteration_core_layout(iteration_path: Path) -> None:
    """
    Create the directories that *every* iteration will populate.

    The decision (``01-decision/``) and code (``02-code/``) phase folders are
    intentionally **not** pre-created: they only exist for iterations that
    actually ran a refinement decision or code regeneration step, and lazily
    creating them keeps ``ls iteration-NNN/`` honest about what happened.
    """
    iteration_path.mkdir(parents=True, exist_ok=True)
    for name in (
        PHASE_SPEC_DIRNAME,
        f"{PHASE_SPEC_DIRNAME}/manifests",
        PHASE_DEPLOY_DIRNAME,
        PHASE_BENCH_DIRNAME,
    ):
        (iteration_path / name).mkdir(parents=True, exist_ok=True)


def clear_bench_dir_if_present(iteration_path: Path) -> None:
    """Remove a finished Locust run from ``05-bench/`` before ``--force`` re-run."""
    bench = iteration_bench_dir(iteration_path)
    if not bench.is_dir():
        return
    has_run = (bench / "config.json").is_file() or (bench / "bench.log").is_file()
    if not has_run:
        return
    for item in list(bench.iterdir()):
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def _copy_tree(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def snapshot_code_dir(iteration_path: Path, code_dir: Path) -> Path | None:
    if not code_dir.is_dir():
        return None
    dest = iteration_code_snapshot_dir(iteration_path)
    dest.mkdir(parents=True, exist_ok=True)
    _copy_tree(code_dir, dest)
    return dest


def snapshot_functional_tests_dir(
    iteration_path: Path, functional_tests_dir: Path
) -> Path | None:
    if not functional_tests_dir.is_dir():
        return None
    dest = iteration_functional_tests_dir(iteration_path)
    dest.mkdir(parents=True, exist_ok=True)
    _copy_tree(functional_tests_dir, dest)
    return dest


def iteration_has_spec(iteration_path: Path) -> bool:
    return find_iteration_spec_path(iteration_path) is not None

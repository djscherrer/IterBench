from __future__ import annotations

from pathlib import Path

import pytest

from k8s_bench.reverify.cleanup import (
    UnsafeDeletionError,
    clear_stale_reverify_artifacts,
    safe_rmtree,
    safe_unlink,
    stale_artifact_targets,
)

from .conftest import make_complete_bench, make_iteration, make_sample_dir, iterations_root_for


def test_safe_rmtree_refuses_target_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "results_reverified"
    root.mkdir()
    outside = tmp_path / "results"  # sibling, NOT under root
    outside.mkdir()
    (outside / "keepme").mkdir()

    with pytest.raises(UnsafeDeletionError):
        safe_rmtree(outside / "keepme", root=root)

    assert (outside / "keepme").is_dir()


def test_safe_unlink_refuses_target_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "results_reverified"
    root.mkdir()
    outside_file = tmp_path / "results" / "important.txt"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(UnsafeDeletionError):
        safe_unlink(outside_file, root=root)

    assert outside_file.is_file()


def test_safe_rmtree_allows_target_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "results_reverified"
    inside = root / "a" / "b"
    inside.mkdir(parents=True)

    safe_rmtree(inside, root=root)

    assert not inside.exists()
    assert root.is_dir()


def test_clear_stale_artifacts_removes_only_generated_phases(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    iteration = make_iteration(it_root, "iteration-003-code")
    make_complete_bench(iteration)
    (iteration / "iteration.log").write_text("old log\n", encoding="utf-8")

    # Artifacts that must survive untouched.
    (iteration / "01-decision").mkdir(parents=True, exist_ok=True)
    (iteration / "01-decision" / "decision.json").write_text("{}\n", encoding="utf-8")
    (iteration / "02-code" / "prompt.log").write_text("prompt\n", encoding="utf-8")
    code_file = iteration / "02-code" / "code" / "app.py"
    spec_file = iteration / "03-spec" / "spec.yaml"
    assert code_file.is_file() and spec_file.is_file()

    removed = clear_stale_reverify_artifacts(iteration, root=results_root)

    assert not (iteration / "04-deploy").exists()
    assert not (iteration / "05-bench").exists()
    assert not (iteration / "iteration.log").exists()
    assert len(removed) == 3

    # Preserved.
    assert code_file.is_file()
    assert spec_file.is_file()
    assert (iteration / "01-decision" / "decision.json").is_file()
    assert (iteration / "02-code" / "prompt.log").is_file()


def test_clear_stale_artifacts_is_a_noop_when_nothing_stale_present(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    iteration = make_iteration(it_root, "iteration-000-baseline")
    # No prior bench/deploy run and no iteration.log -- fresh iteration.
    import shutil

    shutil.rmtree(iteration / "04-deploy")
    shutil.rmtree(iteration / "05-bench")

    removed = clear_stale_reverify_artifacts(iteration, root=results_root)

    assert removed == []
    assert (iteration / "03-spec" / "spec.yaml").is_file()


def test_stale_artifact_targets_names(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    iteration = make_iteration(it_root, "iteration-000-baseline")

    targets = stale_artifact_targets(iteration)

    names = {t.name for t in targets}
    assert names == {"04-deploy", "05-bench", "iteration.log"}

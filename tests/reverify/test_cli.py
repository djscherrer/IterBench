"""
CLI-level tests for scripts/k8s_rebench_results.py.

``test_dry_run_*`` exercise the real discovery + dry-run reporting path with
no mocking at all (dry-run never imports the cluster/orchestration stack).

``test_full_run_*`` mock out the four functions imported from
``k8s_bench.orchestration.*`` / ``k8s_bench.cluster`` so the grouping,
idempotency, and manifest-writing logic in ``main()``/``_run_group`` gets
exercised without Kubernetes, Docker, or Locust.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "k8s_rebench_results.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("k8s_rebench_results", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["k8s_rebench_results"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli():
    return _load_cli_module()


from .conftest import (  # noqa: E402
    make_complete_bench,
    make_iteration,
    make_sample_dir,
    iterations_root_for,
)


def _snapshot_tree(root: Path) -> dict[str, float]:
    """path -> mtime for every file, used to assert dry-run touches nothing."""
    return {
        str(p): p.stat().st_mtime for p in root.rglob("*") if p.is_file()
    }


def test_dry_run_makes_no_filesystem_changes(cli, results_root: Path, caplog) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")
    make_iteration(it_root, "iteration-001-code")

    before = _snapshot_tree(results_root)
    manifest_path = results_root / "reverification_manifest.json"
    assert not manifest_path.exists()

    with caplog.at_level("INFO"):
        rc = cli.main(
            [
                "--results-dir",
                str(results_root),
                "--cluster",
                "test-cluster",
                "--load-profile",
                "default",
                "--dry-run",
            ]
        )

    assert rc == 0
    assert not manifest_path.exists(), "dry-run must not write the manifest"
    assert _snapshot_tree(results_root) == before, "dry-run must not touch any file"
    assert "no cluster contact" in caplog.text
    assert "RUN" in caplog.text


def test_dry_run_reports_already_reverified_as_skip(cli, results_root: Path, caplog) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    iteration = make_iteration(it_root, "iteration-000-baseline")
    make_complete_bench(iteration)

    manifest_path = results_root / "reverification_manifest.json"
    key = cli.manifest_key(
        sample_dir=sample_dir, results_root=results_root, experiment_id="default", iteration_id="iteration-000"
    )
    entries = {
        key: cli.ManifestEntry(
            key=key,
            status="success",
            reason=None,
            original_path=str(iteration),
            task={},
            iteration_id="iteration-000",
            load_profile="default",
            timestamp=cli.utc_now(),
        )
    }
    cli.write_manifest(manifest_path, entries, results_root=results_root, cluster="c", load_profile="default")
    written_at = manifest_path.stat().st_mtime

    with caplog.at_level("INFO"):
        rc = cli.main(
            [
                "--results-dir",
                str(results_root),
                "--cluster",
                "test-cluster",
                "--load-profile",
                "default",
                "--dry-run",
            ]
        )

    assert rc == 0
    assert manifest_path.stat().st_mtime == written_at, "dry-run must not rewrite the manifest"
    assert "SKIP" in caplog.text
    assert "already reverified" in caplog.text


def test_dry_run_with_force_would_rerun_already_reverified(cli, results_root: Path, caplog) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    iteration = make_iteration(it_root, "iteration-000-baseline")
    make_complete_bench(iteration)

    manifest_path = results_root / "reverification_manifest.json"
    key = cli.manifest_key(
        sample_dir=sample_dir, results_root=results_root, experiment_id="default", iteration_id="iteration-000"
    )
    entries = {
        key: cli.ManifestEntry(
            key=key, status="success", reason=None, original_path=str(iteration), task={},
            iteration_id="iteration-000", load_profile="default", timestamp=cli.utc_now(),
        )
    }
    cli.write_manifest(manifest_path, entries, results_root=results_root, cluster="c", load_profile="default")

    with caplog.at_level("INFO"):
        rc = cli.main(
            [
                "--results-dir", str(results_root), "--cluster", "test-cluster",
                "--load-profile", "default", "--dry-run", "--force",
            ]
        )

    assert rc == 0
    assert "RUN" in caplog.text
    assert "would run, 0 would be skipped" in caplog.text


def test_original_results_directory_is_never_touched(cli, tmp_path: Path) -> None:
    """Simulates the documented workflow: `cp -r results results_reverified`,
    then point the tool only at the copy."""
    original = tmp_path / "results"
    sample_dir = make_sample_dir(original)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")

    copy = tmp_path / "results_reverified"
    shutil.copytree(original, copy)

    before_original = _snapshot_tree(original)

    rc = cli.main(
        ["--results-dir", str(copy), "--cluster", "test-cluster", "--load-profile", "default", "--dry-run"]
    )

    assert rc == 0
    assert _snapshot_tree(original) == before_original


def test_missing_results_dir_errors_cleanly(cli, tmp_path: Path) -> None:
    rc = cli.main(
        [
            "--results-dir",
            str(tmp_path / "does-not-exist"),
            "--cluster",
            "test-cluster",
            "--dry-run",
        ]
    )
    assert rc == 2


def test_full_run_records_success_and_is_idempotent_without_force(
    cli, results_root: Path, monkeypatch
) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    iteration = make_iteration(it_root, "iteration-000-baseline")

    calls = {"execute": 0, "preflight": 0, "postlude": 0, "cluster_ready": 0}

    def fake_ensure_ready(*, logger, profile_name):
        calls["cluster_ready"] += 1

    def fake_preflight(task, results_dir, sample, iteration_path, cfg):
        calls["preflight"] += 1
        return object()

    def fake_execute(ctx, iteration_path, cfg):
        calls["execute"] += 1
        return iteration_path / "05-bench"

    def fake_postlude(ctx, **kwargs):
        calls["postlude"] += 1

    import k8s_bench.cluster as cluster_mod
    import k8s_bench.orchestration.preflight as preflight_mod
    import k8s_bench.orchestration.deploy_only as deploy_only_mod

    monkeypatch.setattr(cluster_mod, "ensure_k8s_cluster_ready", fake_ensure_ready)
    monkeypatch.setattr(preflight_mod, "deploy_only_preflight", fake_preflight)
    monkeypatch.setattr(preflight_mod, "sample_postlude", fake_postlude)
    monkeypatch.setattr(deploy_only_mod, "execute_deploy_only_iteration", fake_execute)

    rc = cli.main(
        ["--results-dir", str(results_root), "--cluster", "test-cluster", "--load-profile", "default"]
    )

    assert rc == 0
    assert calls == {"execute": 1, "preflight": 1, "postlude": 1, "cluster_ready": 1}

    manifest_path = results_root / "reverification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = cli.manifest_key(
        sample_dir=sample_dir, results_root=results_root, experiment_id="default", iteration_id="iteration-000"
    )
    assert manifest["iterations"][key]["status"] == "success"

    # Second run, no --force: must skip without calling execute/preflight again.
    rc2 = cli.main(
        ["--results-dir", str(results_root), "--cluster", "test-cluster", "--load-profile", "default"]
    )
    assert rc2 == 0
    assert calls["execute"] == 1, "already-reverified iteration must not be re-run without --force"

    # Third run, with --force: must run again.
    rc3 = cli.main(
        [
            "--results-dir", str(results_root), "--cluster", "test-cluster",
            "--load-profile", "default", "--force",
        ]
    )
    assert rc3 == 0
    assert calls["execute"] == 2


def test_full_run_records_failure_when_deploy_only_returns_none(
    cli, results_root: Path, monkeypatch
) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")

    import k8s_bench.cluster as cluster_mod
    import k8s_bench.orchestration.preflight as preflight_mod
    import k8s_bench.orchestration.deploy_only as deploy_only_mod

    monkeypatch.setattr(cluster_mod, "ensure_k8s_cluster_ready", lambda *, logger, profile_name: None)
    monkeypatch.setattr(preflight_mod, "deploy_only_preflight", lambda *a, **k: object())
    monkeypatch.setattr(preflight_mod, "sample_postlude", lambda *a, **k: None)
    monkeypatch.setattr(deploy_only_mod, "execute_deploy_only_iteration", lambda *a, **k: None)

    rc = cli.main(
        ["--results-dir", str(results_root), "--cluster", "test-cluster", "--load-profile", "default"]
    )

    assert rc == 1
    manifest = json.loads((results_root / "reverification_manifest.json").read_text(encoding="utf-8"))
    key = cli.manifest_key(
        sample_dir=sample_dir, results_root=results_root, experiment_id="default", iteration_id="iteration-000"
    )
    assert manifest["iterations"][key]["status"] == "failed"


def test_full_run_records_skipped_directories_with_reasons(cli, results_root: Path, monkeypatch) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline", spec=False)  # invalid: no spec

    rc = cli.main(
        ["--results-dir", str(results_root), "--cluster", "test-cluster", "--load-profile", "default"]
    )

    assert rc == 0  # nothing to run, but not an error
    manifest = json.loads((results_root / "reverification_manifest.json").read_text(encoding="utf-8"))
    statuses = [e["status"] for e in manifest["iterations"].values()]
    reasons = [e["reason"] for e in manifest["iterations"].values()]
    assert statuses == ["skipped"]
    assert any("spec.yaml" in (r or "") for r in reasons)

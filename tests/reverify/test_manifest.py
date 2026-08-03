from __future__ import annotations

import json
from pathlib import Path

from k8s_bench.reverify.manifest import (
    ManifestEntry,
    already_reverified,
    load_manifest,
    manifest_key,
    path_key,
    utc_now,
    write_manifest,
)

from .conftest import make_sample_dir


def test_manifest_key_stable_across_iteration_folder_rename(results_root: Path) -> None:
    """A failed run renames `iteration-003-code` -> `iteration-003-code-failed`
    (workspace.mark_iteration_folder_failed). The manifest key must be based
    on the logical (sample, experiment, iteration-id) identity, not the raw
    folder name, or a re-run would lose track of the previous attempt."""
    sample_dir = make_sample_dir(results_root)

    key_before = manifest_key(
        sample_dir=sample_dir, results_root=results_root, experiment_id="default", iteration_id="iteration-003"
    )
    key_after_rename = manifest_key(
        sample_dir=sample_dir, results_root=results_root, experiment_id="default", iteration_id="iteration-003"
    )
    assert key_before == key_after_rename


def test_manifest_key_differs_by_sample_and_experiment(results_root: Path) -> None:
    sample0 = make_sample_dir(results_root, sample=0)
    sample1 = make_sample_dir(results_root, sample=1)

    k0 = manifest_key(sample_dir=sample0, results_root=results_root, experiment_id="default", iteration_id="iteration-000")
    k1 = manifest_key(sample_dir=sample1, results_root=results_root, experiment_id="default", iteration_id="iteration-000")
    k0_other_exp = manifest_key(sample_dir=sample0, results_root=results_root, experiment_id="other", iteration_id="iteration-000")

    assert k0 != k1
    assert k0 != k0_other_exp


def test_already_reverified_requires_success_and_matching_profile() -> None:
    entry = ManifestEntry(
        key="k", status="success", reason=None, original_path="p", task={}, iteration_id="iteration-000",
        load_profile="quick-check", timestamp=utc_now(),
    )
    assert already_reverified(entry, load_profile="quick-check") is True
    assert already_reverified(entry, load_profile="default") is False
    assert already_reverified(None, load_profile="quick-check") is False

    failed_entry = ManifestEntry(
        key="k", status="failed", reason="boom", original_path="p", task={}, iteration_id="iteration-000",
        load_profile="quick-check", timestamp=utc_now(),
    )
    assert already_reverified(failed_entry, load_profile="quick-check") is False


def test_write_then_load_manifest_round_trips(tmp_path: Path) -> None:
    manifest_path = tmp_path / "reverification_manifest.json"
    entries = {
        "a::default::iteration-000": ManifestEntry(
            key="a::default::iteration-000", status="success", reason=None,
            original_path="/x/iteration-000", task={"model": "m"}, iteration_id="iteration-000",
            load_profile="default", timestamp=utc_now(), artifacts={"bench_dir": "/x/05-bench"},
        ),
        "a::default::iteration-001": ManifestEntry(
            key="a::default::iteration-001", status="failed", reason="deploy failed",
            original_path="/x/iteration-001", task={"model": "m"}, iteration_id="iteration-001",
            load_profile="default", timestamp=utc_now(),
        ),
    }

    write_manifest(manifest_path, entries, results_root=tmp_path, cluster="test-cluster", load_profile="default")
    reloaded = load_manifest(manifest_path)

    assert set(reloaded) == set(entries)
    assert reloaded["a::default::iteration-000"].status == "success"
    assert reloaded["a::default::iteration-000"].artifacts == {"bench_dir": "/x/05-bench"}
    assert reloaded["a::default::iteration-001"].reason == "deploy failed"

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw["cluster"] == "test-cluster"
    assert raw["load_profile"] == "default"
    assert "iterations" in raw


def test_load_manifest_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "does-not-exist.json") == {}


def test_load_manifest_corrupt_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "reverification_manifest.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_manifest(path) == {}


def test_path_key_is_relative_to_results_root(results_root: Path) -> None:
    target = results_root / "a" / "b" / "c"
    target.mkdir(parents=True)
    key = path_key(target, results_root=results_root)
    assert key == "a/b/c"

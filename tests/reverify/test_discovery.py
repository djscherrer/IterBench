from __future__ import annotations

from pathlib import Path

from env import all_envs
from scenarios import all_scenarios
from k8s_bench.reverify.discovery import (
    DiscoveryFilters,
    discover_iterations,
    group_by_sample_experiment,
)

from .conftest import make_iteration, make_sample_dir, iterations_root_for


def _discover(results_root: Path, **filter_kwargs):
    filters = DiscoveryFilters(**filter_kwargs) if filter_kwargs else None
    return discover_iterations(
        results_root, all_envs=all_envs, all_scenarios=all_scenarios, filters=filters
    )


def test_discovers_non_contiguous_iterations(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")
    make_iteration(it_root, "iteration-001-code")
    # iteration-002..004 never happened (e.g. skipped budget) -- not contiguous
    make_iteration(it_root, "iteration-005-spec")

    report = _discover(results_root)

    assert [d.iteration_id for d in report.discovered] == [
        "iteration-000",
        "iteration-001",
        "iteration-005",
    ]
    assert report.skipped == []


def test_discovery_ordering_is_by_iteration_index_not_directory_listing(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    # Create out of numeric order so a naive `sorted(iterdir())` on folder
    # name text would still happen to work here; use a case that wouldn't:
    # single- vs double/triple-digit folder names sort correctly because the
    # convention zero-pads to 3 digits, so exercise index-based sorting more
    # directly via the grouping helper instead of relying on string luck.
    make_iteration(it_root, "iteration-010-code")
    make_iteration(it_root, "iteration-002-code")
    make_iteration(it_root, "iteration-000-baseline")

    report = _discover(results_root)
    groups = group_by_sample_experiment(report.discovered)
    ((_, _), ordered), = groups.items()

    assert [d.iteration_index for d in ordered] == [0, 2, 10]


def test_missing_spec_is_skipped_with_reason(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline", spec=False)

    report = _discover(results_root)

    assert report.discovered == []
    assert len(report.skipped) == 1
    assert "spec.yaml" in report.skipped[0].reason


def test_missing_code_with_no_earlier_snapshot_is_skipped_with_reason(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline", code=False)

    report = _discover(results_root)

    assert report.discovered == []
    assert len(report.skipped) == 1
    assert "code snapshot" in report.skipped[0].reason


def test_missing_code_falls_back_to_earlier_iteration_snapshot(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline", code=True)
    # a spec-only refinement: no code of its own, must inherit iteration-000's
    make_iteration(it_root, "iteration-001-spec", code=False)

    report = _discover(results_root)

    assert [d.iteration_id for d in report.discovered] == ["iteration-000", "iteration-001"]
    inherited = next(d for d in report.discovered if d.iteration_id == "iteration-001")
    assert inherited.code_dir == (it_root / "iteration-000-baseline" / "02-code" / "code")


def test_failed_folder_excluded_by_default_included_with_flag(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")
    make_iteration(it_root, "iteration-001-code-failed")

    report_default = _discover(results_root)
    assert [d.iteration_id for d in report_default.discovered] == ["iteration-000"]
    failed_skip = next(s for s in report_default.skipped if "iteration-001" in str(s.path))
    assert "failed" in failed_skip.reason
    assert "--include-failed" in failed_skip.reason

    report_included = _discover(results_root, include_failed=True)
    assert [d.iteration_id for d in report_included.discovered] == [
        "iteration-000",
        "iteration-001",
    ]
    failed_entry = next(d for d in report_included.discovered if d.iteration_id == "iteration-001")
    assert failed_entry.is_failed_folder is True


def test_unrecognized_folder_name_is_skipped(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")
    (it_root / "not-an-iteration-dir").mkdir()

    report = _discover(results_root)

    assert [d.iteration_id for d in report.discovered] == ["iteration-000"]
    bogus_skip = next(s for s in report.skipped if "not-an-iteration-dir" in str(s.path))
    assert "not a recognized" in bogus_skip.reason


def test_unresolvable_task_metadata_skips_every_child_with_reason(results_root: Path) -> None:
    bad_sample = (
        results_root / "some-model" / "NotAScenario" / "Go-net-http" / "temp0.2-openapi-none" / "sample0"
    )
    it_root = iterations_root_for(bad_sample)
    make_iteration(it_root, "iteration-000-baseline")

    report = _discover(results_root)

    assert report.discovered == []
    assert len(report.skipped) == 1
    assert "could not derive task metadata" in report.skipped[0].reason


def test_unrelated_iterations_directory_outside_k8s_experiments_is_ignored(results_root: Path) -> None:
    # Some other tool's "iterations" directory that happens to share the name
    # but does not sit under k8s-experiments/<slug>/ -- must not be treated
    # as a BaxBench iteration workspace at all (not even as a skip).
    rogue = results_root / "some-model" / "unrelated" / "iterations"
    rogue.mkdir(parents=True)
    (rogue / "iteration-000").mkdir()

    report = _discover(results_root)

    assert report.discovered == []
    assert report.skipped == []


def test_filters_are_reported_as_skips_not_silently_dropped(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root, sample=0)
    it_root = iterations_root_for(sample_dir)
    make_iteration(it_root, "iteration-000-baseline")
    make_iteration(it_root, "iteration-001-code")

    report = _discover(results_root, iterations=frozenset({"iteration-000"}))

    assert [d.iteration_id for d in report.discovered] == ["iteration-000"]
    excluded = next(s for s in report.skipped if "iteration-001" in str(s.path))
    assert excluded.reason == "excluded by filter"


def test_sample_and_scenario_filters(results_root: Path) -> None:
    s0 = make_sample_dir(results_root, sample=0)
    s1 = make_sample_dir(results_root, sample=1)
    make_iteration(iterations_root_for(s0), "iteration-000-baseline")
    make_iteration(iterations_root_for(s1), "iteration-000-baseline")

    report = _discover(results_root, samples=frozenset({1}))

    assert len(report.discovered) == 1
    assert report.discovered[0].task.sample == 1

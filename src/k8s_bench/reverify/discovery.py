"""
Recursive discovery of re-benchable k8s iteration directories under a results tree.

Pure filesystem inspection — never touches Kubernetes, Docker, or the LLM
stack — so it is safe to call from ``--dry-run`` and from unit tests alike.

An iteration is "re-benchable" when all of the following hold:

- its folder name parses as ``iteration-NNN`` (optionally suffixed
  ``-baseline``/``-spec``/``-code``, optionally ``-failed``);
- it is not a ``-failed`` folder, unless ``include_failed`` is requested;
- it has a usable ``03-spec/spec.yaml``
  (:func:`workspace.find_iteration_spec_path`);
- it has an application code snapshot, either its own or via the existing
  code-lineage fallback (:func:`workspace.resolve_bench_rebuild_code_dir`);
- its path maps back to a known BaxBench scenario and environment
  (:mod:`k8s_bench.reverify.metadata`).

Directories that fail any of these checks are never silently dropped: they
are collected as :class:`SkippedIteration` with a human-readable reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from env.base import Env
from scenarios.base import Scenario
from workspace import (
    K8S_EXPERIMENTS_DIRNAME,
    ITERATIONS_DIRNAME,
    find_iteration_spec_path,
    iteration_folder_is_failed,
    iteration_id_for_index,
    parse_iteration_index,
    resolve_bench_rebuild_code_dir,
)

from .metadata import TaskMetadata, parse_task_metadata


@dataclass(frozen=True)
class DiscoveredIteration:
    path: Path
    sample_dir: Path
    experiment_id: str
    task: TaskMetadata
    iteration_index: int
    iteration_id: str  # normalized, e.g. "iteration-003" (no folder-kind suffix)
    is_failed_folder: bool
    code_dir: Path
    spec_path: Path


@dataclass(frozen=True)
class SkippedIteration:
    path: Path
    reason: str


@dataclass
class DiscoveryReport:
    discovered: list[DiscoveredIteration] = field(default_factory=list)
    skipped: list[SkippedIteration] = field(default_factory=list)


@dataclass(frozen=True)
class DiscoveryFilters:
    models: frozenset[str] | None = None
    scenarios: frozenset[str] | None = None
    envs: frozenset[str] | None = None
    samples: frozenset[int] | None = None
    experiments: frozenset[str] | None = None
    iterations: frozenset[str] | None = None  # normalized ids, e.g. "iteration-003"
    include_failed: bool = False

    def matches_task(
        self,
        iteration_id: str,
        *,
        model: str,
        scenario_id: str,
        env_id: str,
        sample: int,
        experiment_id: str,
    ) -> bool:
        if self.models is not None and model not in self.models:
            return False
        if self.scenarios is not None and scenario_id not in self.scenarios:
            return False
        if self.envs is not None and env_id not in self.envs:
            return False
        if self.samples is not None and sample not in self.samples:
            return False
        if self.experiments is not None and experiment_id not in self.experiments:
            return False
        if self.iterations is not None and iteration_id not in self.iterations:
            return False
        return True


def _discovery_sort_key(d: DiscoveredIteration) -> tuple:
    t = d.task
    return (
        t.model,
        t.scenario.id,
        t.env.id,
        t.temperature,
        t.spec_type,
        t.safety_prompt,
        t.sample,
        d.experiment_id,
        d.iteration_index,
        d.is_failed_folder,
        d.path.name,
    )


def discover_iterations(
    results_root: Path,
    *,
    all_envs: Sequence[Env],
    all_scenarios: Sequence[Scenario],
    filters: DiscoveryFilters | None = None,
) -> DiscoveryReport:
    """Walk ``results_root`` and classify every iteration folder found."""
    filters = filters or DiscoveryFilters()
    report = DiscoveryReport()
    results_root = results_root.expanduser().resolve()

    iterations_dirs = sorted(
        p for p in results_root.rglob(ITERATIONS_DIRNAME) if p.is_dir()
    )
    for iterations_dir in iterations_dirs:
        experiment_root = iterations_dir.parent
        if experiment_root.parent.name != K8S_EXPERIMENTS_DIRNAME:
            # Not a k8s-bench experiment workspace (e.g. an unrelated directory
            # that happens to be named "iterations"). Skip quietly: this is not
            # a BaxBench artifact at all, not a re-benchable-but-broken one.
            continue
        experiment_id = experiment_root.name
        sample_dir = experiment_root.parent.parent

        try:
            task = parse_task_metadata(
                sample_dir, all_envs=all_envs, all_scenarios=all_scenarios
            )
        except ValueError as exc:
            for child in sorted(iterations_dir.iterdir()):
                if child.is_dir():
                    report.skipped.append(
                        SkippedIteration(child, f"could not derive task metadata: {exc}")
                    )
            continue

        for child in sorted(iterations_dir.iterdir()):
            if not child.is_dir():
                continue

            idx = parse_iteration_index(child.name)
            if idx is None:
                report.skipped.append(
                    SkippedIteration(child, "not a recognized 'iteration-NNN' folder name")
                )
                continue

            is_failed = iteration_folder_is_failed(child.name)
            if is_failed and not filters.include_failed:
                report.skipped.append(
                    SkippedIteration(
                        child,
                        "failed iteration folder (pass --include-failed to consider it)",
                    )
                )
                continue

            spec_path = find_iteration_spec_path(child)
            if spec_path is None:
                report.skipped.append(
                    SkippedIteration(child, "missing 03-spec/spec.yaml")
                )
                continue

            code_dir = resolve_bench_rebuild_code_dir(
                sample_dir, child, experiment_id=experiment_id
            )
            if code_dir is None:
                report.skipped.append(
                    SkippedIteration(
                        child,
                        "no application code snapshot on this iteration or any "
                        "earlier one in the same experiment",
                    )
                )
                continue

            iid = iteration_id_for_index(idx)
            if not filters.matches_task(
                iid,
                model=task.model,
                scenario_id=task.scenario.id,
                env_id=task.env.id,
                sample=task.sample,
                experiment_id=experiment_id,
            ):
                report.skipped.append(SkippedIteration(child, "excluded by filter"))
                continue

            report.discovered.append(
                DiscoveredIteration(
                    path=child,
                    sample_dir=sample_dir,
                    experiment_id=experiment_id,
                    task=task,
                    iteration_index=idx,
                    iteration_id=iid,
                    is_failed_folder=is_failed,
                    code_dir=code_dir,
                    spec_path=spec_path,
                )
            )

    report.discovered.sort(key=_discovery_sort_key)
    return report


def group_by_sample_experiment(
    discovered: list[DiscoveredIteration],
) -> dict[tuple[Path, str], list[DiscoveredIteration]]:
    """Group discovered iterations by (sample_dir, experiment_id), each list ordered by iteration index."""
    groups: dict[tuple[Path, str], list[DiscoveredIteration]] = {}
    for d in discovered:
        groups.setdefault((d.sample_dir, d.experiment_id), []).append(d)
    for key, items in groups.items():
        items.sort(key=lambda d: (d.iteration_index, d.is_failed_folder, d.path.name))
    return groups


__all__ = [
    "DiscoveredIteration",
    "SkippedIteration",
    "DiscoveryReport",
    "DiscoveryFilters",
    "discover_iterations",
    "group_by_sample_experiment",
]

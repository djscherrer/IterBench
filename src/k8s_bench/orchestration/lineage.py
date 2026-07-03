"""Load disk-derived lineage for one iteration (read once in ``plan_iteration``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..code.prior import find_latest_prior_code_failure
from ..feedback import IterationFeedback, load_prior_feedback_for_iteration
from ..failure import FailureRecord
from ..workspace import (
    iteration_id_for_index,
    latest_spec_path,
    parse_iteration_index,
)


@dataclass(frozen=True)
class SpecRef:
    spec_path: Path
    iteration_dir: Path
    iteration_id: str


@dataclass(frozen=True)
class IterationLineage:
    """
    Snapshot of experiment disk state before routing this iteration.

    - ``bench_feedback``: iteration N−1 summary for the decision prompt.
    - ``prior_code_dir``: ``02-code/code/`` from iteration N−1 (deployment copy).
    - ``latest_spec``: newest ``spec.yaml`` on disk (incl. failed folders).
    - ``prior_code_failure``: terminal code failure from a prior ``*-code-failed`` iter.
    """

    bench_feedback: IterationFeedback | None
    prior_code_dir: Path | None
    latest_spec: SpecRef | None
    prior_code_failure: FailureRecord | None


def load_iteration_lineage(
    sample_dir: Path,
    iteration_index: int,
    *,
    is_baseline: bool,
    experiment_id: str | None = None,
) -> IterationLineage:
    bench_feedback: IterationFeedback | None = None
    if not is_baseline:
        bench_feedback = load_prior_feedback_for_iteration(
            sample_dir,
            iteration_index,
            experiment_id=experiment_id,
        )

    from ..workspace import prior_iteration_code_dir

    prior_code_dir = prior_iteration_code_dir(
        sample_dir, iteration_index, experiment_id=experiment_id
    )

    latest_spec: SpecRef | None = None
    spec_pair = latest_spec_path(sample_dir, experiment_id=experiment_id)
    if spec_pair is not None:
        spec_path, iteration_dir = spec_pair
        latest_spec = SpecRef(
            spec_path=spec_path,
            iteration_dir=iteration_dir,
            iteration_id=_logical_iteration_id(iteration_dir.name),
        )

    prior_code_failure: FailureRecord | None = None
    if not is_baseline:
        prior_code_failure = find_latest_prior_code_failure(
            sample_dir,
            current_iteration_index=iteration_index,
            experiment_id=experiment_id,
        )

    return IterationLineage(
        bench_feedback=bench_feedback,
        prior_code_dir=prior_code_dir,
        latest_spec=latest_spec,
        prior_code_failure=prior_code_failure,
    )


def _logical_iteration_id(folder_or_id: str) -> str:
    idx = parse_iteration_index(folder_or_id)
    if idx is not None:
        return iteration_id_for_index(idx)
    return folder_or_id

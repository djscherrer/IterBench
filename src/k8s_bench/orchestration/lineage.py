"""Load disk-derived lineage for one iteration (read once in ``plan_iteration``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..feedback import IterationFeedback, load_prior_feedback_for_iteration
from ..failure import (
    CodeFailureRecord,
    IterationFailure,
    SpecFailureRecord,
    load_prior_iteration_failure,
)
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

    - ``bench_feedback``: iteration N−1 Locust/diagnostics summary when N−1
      completed a successful bench run (never a failure narrative).
    - ``prior_iteration_failure``: iteration N−1 ``failure.json`` envelope when
      N−1 failed (code, spec, deploy, or bench). Older failures are not carried
      forward — they remain in conversation history only.
    - ``prior_code_dir``: ``02-code/code/`` from iteration N−1 (deployment copy).
    - ``latest_spec``: newest ``spec.yaml`` on disk (incl. failed folders).
    """

    bench_feedback: IterationFeedback | None
    prior_iteration_failure: IterationFailure | None
    prior_code_dir: Path | None
    latest_spec: SpecRef | None


def prior_code_failure_record(lineage: IterationLineage) -> CodeFailureRecord | None:
    failure = lineage.prior_iteration_failure
    if failure is None or failure.phase != "code":
        return None
    terminal = failure.terminal
    return terminal if isinstance(terminal, CodeFailureRecord) else None


def prior_spec_failure_record(lineage: IterationLineage) -> SpecFailureRecord | None:
    failure = lineage.prior_iteration_failure
    if failure is None or failure.phase != "spec":
        return None
    terminal = failure.terminal
    return terminal if isinstance(terminal, SpecFailureRecord) else None


def lineage_based_on_iteration_id(lineage: IterationLineage) -> str | None:
    if lineage.bench_feedback is not None:
        return lineage.bench_feedback.iteration_id
    if lineage.prior_iteration_failure is not None:
        return lineage.prior_iteration_failure.iteration_id
    return None


def load_iteration_lineage(
    sample_dir: Path,
    iteration_index: int,
    *,
    is_baseline: bool,
    experiment_id: str | None = None,
) -> IterationLineage:
    bench_feedback: IterationFeedback | None = None
    prior_iteration_failure: IterationFailure | None = None
    if not is_baseline:
        bench_feedback = load_prior_feedback_for_iteration(
            sample_dir,
            iteration_index,
            experiment_id=experiment_id,
        )
        prior_iteration_failure = load_prior_iteration_failure(
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

    return IterationLineage(
        bench_feedback=bench_feedback,
        prior_iteration_failure=prior_iteration_failure,
        prior_code_dir=prior_code_dir,
        latest_spec=latest_spec,
    )


def _logical_iteration_id(folder_or_id: str) -> str:
    idx = parse_iteration_index(folder_or_id)
    if idx is not None:
        return iteration_id_for_index(idx)
    return folder_or_id

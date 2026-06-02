"""
Frozen dataclasses scoped to each level of the benchmark loop.

Filesystem is the **single source of truth**; these dataclasses are short-lived
projections of disk state for the duration of one logical scope:

- :class:`RunConfig`   – built once per ``run_iterative_k8s_bench`` call.
- :class:`SampleContext` – built once per sample (after FT gate + image build).
- :class:`PriorIteration` – built per iteration by re-reading disk.
- :class:`IterationPlan` – built per iteration by ``plan.plan_iteration``.
- :class:`IterationOutcome` – return value of ``execute.execute_iteration``;
                              the ``abort_sample`` flag preserves the legacy
                              ``break`` semantics on baseline failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

from ..feedback import IterationFeedback
from ..functional_failure import FunctionalFailureReport
from ..refinement.decision import RefinementDecision, RefinementMode


RefinementAction = Literal["baseline", "code", "deployment"]
BaselineCodeMode = Literal["reuse", "regenerate"]


@dataclass(frozen=True)
class RunConfig:
    load_profile: str
    experiment_id: str
    refinement_mode: RefinementMode
    iteration_ids: list[str]
    total_iterations: int
    timeout: int
    ft_timeout: int
    k8s_wait_timeout: int
    bench_users: int | None
    bench_spawn_rate: int | None
    bench_run_time: int | None
    num_ports: int
    min_port: int
    vllm_port: int
    max_retries: int
    base_delay: float
    max_delay: float
    force: bool
    # Baseline (iteration-000) code source. ``reuse`` (default) takes the
    # sample-level ``code/`` snapshot from ``--mode generate``; ``regenerate``
    # runs a fresh LLM codegen + FT loop into ``iteration-000-baseline/02-code/``
    # before iteration-000 spec/bench. See ``baseline.codegen`` for details.
    baseline_code_mode: BaselineCodeMode = "reuse"
    # Maximum LLM codegen attempts during baseline ``regenerate`` mode. Only
    # consulted when ``baseline_code_mode == "regenerate"``.
    baseline_code_max_attempts: int = 3
    # Maximum baseline spec-generation attempts (LLM call + static validation
    # + deploy probe). Applies to iteration-000 regardless of
    # ``baseline_code_mode``; refinement iterations use a single attempt and
    # rely on subsequent iterations for recovery.
    baseline_spec_max_attempts: int = 5


@dataclass(frozen=True)
class SampleContext:
    task: Any
    results_dir: Path
    sample: int
    sample_dir: Path
    save_dir: Path
    base_image_id: str


@dataclass(frozen=True)
class PriorIteration:
    """Signals from iterations preceding this one (loaded from disk)."""

    bench_feedback: IterationFeedback | None
    failure_report: FunctionalFailureReport | None


@dataclass(frozen=True)
class IterationPlan:
    """What this iteration will do, decided once at the start."""

    iteration_id: str
    iteration_index: int
    refinement_action: RefinementAction
    decision: RefinementDecision | None
    prior: PriorIteration
    reuse_spec_from: str | None
    source_code_dir: Path


class IterationOutcome(NamedTuple):
    run_dir: Path | None
    abort_sample: bool

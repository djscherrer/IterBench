"""
Frozen dataclasses scoped to each level of the benchmark loop.

Filesystem is the **single source of truth**; these dataclasses are short-lived
projections of disk state for the duration of one logical scope:

- :class:`RunConfig`   – built once per iterative experiment run.
- :class:`SampleContext` – built once per sample in preflight; ``base_image_id``
  is set after iteration-000 baseline codegen completes.
- :class:`IterationLineage` – built per iteration by ``plan.plan_iteration``.
- :class:`IterationSetup` – built per iteration by ``plan.plan_iteration``.
- :class:`IterationPlan` – built per iteration by the orchestrator (``orchestration.execute``).
- :class:`IterationOutcome` – return value of ``execute.execute_iteration``;
                              the ``abort_sample`` flag preserves the legacy
                              ``break`` semantics on baseline failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple, TYPE_CHECKING

from ..stages.decision import RefinementDecision, RefinementMode
from .lineage import IterationLineage, SpecRef

if TYPE_CHECKING:
    from llm import Prompter


RefinementAction = Literal["baseline", "code", "deployment"]


@dataclass(frozen=True)
class RunConfig:
    load_profile: str
    experiment_id: str
    k8s_cluster: str
    llm_max_cost_usd: float | None
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
    # Maximum LLM codegen attempts for iteration-000 baseline (FT-validated).
    baseline_code_max_attempts: int = 3
    # Maximum baseline spec-generation attempts (LLM call + static validation
    # + deploy probe). Refinement iterations use a single attempt.
    baseline_spec_max_attempts: int = 5


@dataclass(frozen=True)
class SampleContext:
    task: Any
    results_dir: Path
    sample: int
    sample_dir: Path
    task_run_dir: Path  # task.get_save_dir(); parent of all sampleN/ for this config
    base_image_id: str | None = None
    experiment_id: str = "default"
    k8s_cluster: str = ""
    llm_max_cost_usd: float | None = None
    session: "Prompter | None" = None


@dataclass(frozen=True)
class IterationSetup:
    """Per-iteration inputs before the decision stage runs."""

    iteration_id: str
    iteration_index: int
    iteration_path: Path
    lineage: IterationLineage
    is_baseline: bool


@dataclass(frozen=True)
class IterationPlan:
    """What this iteration will do, decided once at the start."""

    iteration_id: str
    iteration_index: int
    refinement_action: RefinementAction
    decision: RefinementDecision | None
    lineage: IterationLineage


class IterationOutcome(NamedTuple):
    run_dir: Path | None
    abort_sample: bool
    base_image_id: str | None = None

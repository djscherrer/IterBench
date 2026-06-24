"""
Run one iteration end-to-end: decision → code → spec → deploy → bench → outcome.

The orchestrator wires together the stages defined in :mod:`k8s_bench.stages`.
Each phase owns its **own** log file rooted at the matching ``NN-<phase>/``
folder, so a reader of ``iteration-NNN-*/`` can tell at a glance which step
emitted which line. ``iteration.log`` at the iteration root holds only the
header + outcome (cheap, scannable index).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..stages.bench import run_bench
from ..stages.code import run_code_stage
from ..stages.decision import run_decision_stage
from ..stages.deploy import run_deploy_stage
from ..stages.outcome import record_outcome
from ..stages.spec import prepare_spec_or_fail
from ..workspace import (
    clear_bench_dir_if_present,
    iteration_bench_dir,
    iteration_log_path,
    iteration_spec_log_path,
    resolve_iteration_dir,
)
from .config import (
    IterationOutcome,
    RunConfig,
    SampleContext,
)
from .plan import plan_iteration


def execute_iteration(
    ctx: SampleContext,
    iteration_index: int,
    iteration_id: str,
    cfg: RunConfig,
) -> IterationOutcome:
    """Plan → decision → code → spec → deploy → bench → record outcome."""
    setup = plan_iteration(ctx, iteration_index, iteration_id, cfg)
    if setup is None:
        return IterationOutcome(None, False)

    plan = run_decision_stage(ctx, setup, cfg)
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    _write_iteration_header(iteration_path, plan, cfg)

    code_result = run_code_stage(ctx, plan, cfg)
    if code_result.image_id is None:
        _append_iteration_outcome(
            resolve_iteration_dir(ctx.sample_dir, plan.iteration_id),
            "code-failed",
        )
        return IterationOutcome(None, code_result.abort_sample)

    image_id = code_result.image_id
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    run_dir = _prepare_run_dir(iteration_path, cfg)

    spec_log = iteration_spec_log_path(iteration_path)
    with ctx.task.create_logger(spec_log) as spec_logger:
        spec_file, abort_sample = prepare_spec_or_fail(
            ctx, plan, image_id, cfg, spec_logger
        )
    if spec_file is None:
        _append_iteration_outcome(
            resolve_iteration_dir(ctx.sample_dir, plan.iteration_id),
            "spec-failed",
        )
        return IterationOutcome(None, abort_sample)

    deploy_result = run_deploy_stage(ctx, plan, image_id, cfg)
    if not deploy_result.ok:
        _append_iteration_outcome(
            resolve_iteration_dir(ctx.sample_dir, plan.iteration_id),
            "deploy-failed",
        )
        return IterationOutcome(None, False)

    bench_log = run_dir / "bench.log"
    with ctx.task.create_logger(bench_log) as bench_logger:
        run_bench(ctx, plan, run_dir, image_id, cfg, bench_logger)
        record_outcome(ctx, plan, run_dir, spec_file, cfg, bench_logger)

    _append_iteration_outcome(iteration_path, "ok")
    return IterationOutcome(run_dir, False, image_id)


def _write_iteration_header(
    iteration_path: Path, plan, cfg: RunConfig
) -> None:
    """One-line iteration header written to ``iteration.log`` for quick scanning."""
    path = iteration_log_path(iteration_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    path.write_text(
        f"{ts} k8s iterative iteration {plan.iteration_index}/"
        f"{len(cfg.iteration_ids) - 1} experiment={cfg.experiment_id} "
        f"iteration={plan.iteration_id} refinement={cfg.refinement_mode} "
        f"action={plan.refinement_action}\n",
        encoding="utf-8",
    )


def _append_iteration_outcome(iteration_path: Path, outcome: str) -> None:
    path = iteration_log_path(iteration_path)
    if not path.is_file():
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} outcome={outcome}\n")


def _prepare_run_dir(iteration_path: Path, cfg: RunConfig) -> Path:
    run_dir = iteration_bench_dir(iteration_path)
    if cfg.force:
        clear_bench_dir_if_present(iteration_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

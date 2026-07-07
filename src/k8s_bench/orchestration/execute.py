"""
Run one iteration end-to-end: decision → code → spec → deploy → bench → outcome.

The orchestrator wires together the stages defined in :mod:`k8s_bench.stages`.
Each stage receives a logger created here (``NN-<phase>/phase.log`` for decision
through deploy; ``05-bench/bench.log`` for bench via :func:`iteration_bench_log_path`).
``iteration.log`` at the iteration root holds only the header + outcome
(cheap, scannable index).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..stages.bench import run_bench_stage
from ..stages.code import run_code_stage, run_reuse_code_stage
from ..stages.decision import run_decision_stage
from ..stages.deploy import run_deploy_stage
from ..stages.outcome import run_outcome_stage
from ..stages.spec import run_reuse_spec_stage, run_spec_stage
from ..workspace import (
    clear_bench_dir_if_present,
    iteration_bench_dir,
    iteration_bench_log_path,
    iteration_code_log_path,
    iteration_decision_log_path,
    iteration_deploy_log_path,
    iteration_log_path,
    iteration_spec_log_path,
    resolve_iteration_dir,
)
from .config import (
    IterationOutcome,
    RunConfig,
    SampleContext,
)
from .plan import finalize_iteration_plan, plan_iteration


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

    # --------------------
    # 01-decision (routing)
    # --------------------
    with ctx.task.create_logger(
        iteration_decision_log_path(setup.iteration_path)
    ) as decision_logger:
        decision_result = run_decision_stage(ctx, setup, cfg, decision_logger)

    if decision_result.abort_sample:
        return IterationOutcome(None, True)

    iteration_path, plan = finalize_iteration_plan(ctx, setup, decision_result)
    _write_iteration_header(iteration_path, plan, cfg)

    refinement_action = plan.refinement_action
    lineage = plan.lineage

    # -------
    # 02-code
    # -------
    with ctx.task.create_logger(
        iteration_code_log_path(iteration_path)
    ) as code_logger:
        if refinement_action == "deployment":
            code_result = run_reuse_code_stage(ctx, plan, code_logger)
        else:
            code_mode = "baseline" if refinement_action == "baseline" else "refinement"
            max_attempts = (
                cfg.baseline_code_max_attempts if refinement_action == "baseline" else 1
            )
            abort_sample_on_fail = refinement_action == "baseline"
            code_result = run_code_stage(
                ctx,
                plan,
                cfg,
                code_logger,
                mode=code_mode,  # type: ignore[arg-type]
                max_attempts=max_attempts,
                abort_sample_on_fail=abort_sample_on_fail,
            )
    if code_result.image_id is None:
        _append_iteration_outcome(iteration_path, "code-failed")
        return IterationOutcome(None, code_result.abort_sample)

    image_id = code_result.image_id
    run_dir = _prepare_run_dir(iteration_path, cfg)

    # -------
    # 03-spec
    # -------
    with ctx.task.create_logger(
        iteration_spec_log_path(iteration_path)
    ) as spec_logger:
        if refinement_action == "code" and lineage.latest_spec is not None:
            spec_result = run_reuse_spec_stage(
                ctx,
                plan,
                source_iteration_id=lineage.latest_spec.iteration_id,
                logger=spec_logger,
            )
        else:
            spec_mode = "baseline" if refinement_action == "baseline" else "refinement"
            max_attempts = (
                cfg.baseline_spec_max_attempts if refinement_action == "baseline" else 1
            )
            spec_result = run_spec_stage(
                ctx,
                plan,
                image_id,
                cfg,
                spec_logger,
                mode=spec_mode,
                max_attempts=max_attempts,
            )
    if spec_result.spec_file is None:
        _append_iteration_outcome(iteration_path, "spec-failed")
        return IterationOutcome(None, spec_result.abort_sample)

    # ---------
    # 04-deploy
    # ---------
    with ctx.task.create_logger(
        iteration_deploy_log_path(iteration_path)
    ) as deploy_logger:
        deploy_result = run_deploy_stage(ctx, plan, image_id, cfg, deploy_logger)
    if not deploy_result.ok:
        _append_iteration_outcome(iteration_path, "deploy-failed")
        return IterationOutcome(None, False)

    # --------
    # 05-bench
    # --------
    with ctx.task.create_logger(
        iteration_bench_log_path(iteration_path)
    ) as bench_logger:
        bench_result = run_bench_stage(ctx, plan, run_dir, cfg, bench_logger)
        if not bench_result.ok:
            _append_iteration_outcome(iteration_path, "bench-failed")
            return IterationOutcome(None, False)
        # ---------
        # 06-outcome
        # ---------
        run_outcome_stage(
            ctx, plan, run_dir, spec_result.spec_file, cfg, bench_logger
        )

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

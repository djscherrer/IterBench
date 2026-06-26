"""
Outcome stage.

Reads the bench artifacts written by the bench stage, builds an
:class:`IterationFeedback`, persists it via the workspace artifact helpers,
updates ``meta.json`` to ``status="success"``, and appends a perf-run block to
``experiment_summary.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..experiment_summary import append_perf_run_block
from ..feedback import collect_iteration_feedback
from ..spec.models import K8sWorkloadSpec
from ..workspace import (
    resolve_iteration_dir,
    update_iteration_meta,
    write_feedback,
)
from ..orchestration.config import IterationPlan, RunConfig, SampleContext


def run_outcome_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    spec_file: Path,
    cfg: RunConfig,
    logger: logging.Logger,
) -> None:
    """Build feedback, persist artifacts, update meta + summary."""
    iteration_path = resolve_iteration_dir(ctx.sample_dir, plan.iteration_id)
    try:
        spec = K8sWorkloadSpec.from_yaml_file(spec_file)
        fb = collect_iteration_feedback(
            perf_run_dir=run_dir,
            iteration_path=iteration_path,
            namespace=spec.namespace,
            logger=logger,
        )
        write_feedback(run_dir, fb)
        update_iteration_meta(
            iteration_path,
            status="success",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            summary_path = append_perf_run_block(
                sample_dir=ctx.sample_dir,
                iteration_id=plan.iteration_id,
                perf_run_dir=run_dir,
                feedback=fb,
                load_profile=cfg.load_profile,
            )
            logger.info("Updated experiment summary: %s", summary_path)
        except Exception as exc:
            logger.warning("Could not update experiment summary: %s", exc)
    except Exception as exc:
        logger.warning("Could not write iteration feedback: %s", exc)

"""
Deploy stage (``04-deploy/``): bring the iteration up on the cluster.

Renders the spec with the bench image, applies manifests, waits for readiness,
and records ``04-deploy/probe.json``. A passing probe leaves a live namespace
that the bench stage can reuse instead of redeploying.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cluster.deploy import DeployResult, deploy_iteration
from ..cluster.images import prepare_image_for_k8s
from ..cluster.load_target import resolve_nodeport_target
from ..cluster.profiles import selected_cluster_profile
from ..failure import DeployFailureRecord, fail_iteration_phase
from ..failure.persist import build_deploy_iteration_failure
from ..iteration import ensure_iteration_spec
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import deploy_probe_record_path, iteration_deploy_dir, resolve_iteration_dir


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeployProbeResult:
    ok: bool
    deploy_result: DeployResult | None
    reason: str
    details: dict[str, Any]


@dataclass(frozen=True)
class DeployAttemptResult:
    """Outcome of one deploy-probe attempt."""

    ok: bool = False
    reason: str = ""
    failure: DeployFailureRecord | None = None


@dataclass(frozen=True)
class DeployStageResult:
    """Success when ``reason`` is ``None``; ``skipped`` is set on baseline skip paths."""

    skipped: bool = False
    reason: str | None = None


# ---------------------------------------------------------------------------
# Attempt-level helpers
# ---------------------------------------------------------------------------

def deploy_failure_record_from_probe(
    iteration_id: str,
    probe: DeployProbeResult,
    *,
    attempt: int | None = None,
) -> DeployFailureRecord:
    details = dict(probe.details or {})
    if probe.deploy_result is not None and probe.deploy_result.wait_details:
        for resource, detail in probe.deploy_result.wait_details.items():
            details[f"wait/{resource}"] = str(detail)[:500]
    return DeployFailureRecord(
        phase="deploy",
        kind="deploy_probe",
        iteration_id=iteration_id,
        attempt=attempt,
        summary=probe.reason,
        reason=probe.reason,
        details=details,
    )


def rotate_top_level_into_attempt(
    iteration_path: Path,
    attempt_dir: Path,
) -> None:
    """Move the current ``04-deploy/`` snapshot into ``attempts/<NNN>/``."""
    phase_dir = iteration_deploy_dir(iteration_path)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name in ("probe.json", "bench.json"):
        src = phase_dir / name
        if src.is_file():
            shutil.move(str(src), str(attempt_dir / name))


def _kubectl(
    args: list[str], *, timeout_s: int | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def check_service_endpoints_ready(
    *,
    namespace: str,
    service: str,
    logger: logging.Logger | None = None,
) -> tuple[bool, str]:
    log = logger or logging.getLogger(__name__)
    proc = _kubectl(
        ["get", "endpoints", service, "-n", namespace, "-o", "json"],
        timeout_s=30,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "unknown error").strip()
        log.warning("endpoints check failed for %s/%s: %s", namespace, service, msg)
        return False, f"endpoints/{service} unavailable: {msg}"

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return False, f"endpoints/{service} response not JSON: {exc}"

    ready_count = 0
    for subset in data.get("subsets") or []:
        for addr in subset.get("addresses") or []:
            if addr.get("ip"):
                ready_count += 1
    if ready_count < 1:
        return False, f"endpoints/{service} has no ready addresses"
    return True, f"{ready_count} ready endpoint(s)"


def probe_record_passed(iteration_path: Path) -> bool:
    probe_path = deploy_probe_record_path(iteration_path)
    if not probe_path.is_file():
        return False
    try:
        record = json.loads(probe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(record.get("success"))


def run_deploy_attempt(
    *,
    iteration_path: Path,
    iteration_id: str,
    image_id: str,
    sample_slug: str,
    app_port: int,
    needs_db: bool,
    k8s_cluster: str,
    wait_timeout_s: int,
    labels: dict[str, str] | None,
    logger: logging.Logger,
    attempt_index: int = 1,
    enable_attempts: bool = False,
    persist_on_failure: bool = True,
) -> DeployAttemptResult:
    """Push image, apply manifests, and verify the iteration is reachable."""
    details: dict[str, Any] = {}
    deploy_result: DeployResult | None = None

    try:
        prepared = prepare_image_for_k8s(
            image_id,
            sample_slug=sample_slug,
            profile_name=k8s_cluster,
            logger=logger,
        )
        spec = ensure_iteration_spec(
            iteration_path,
            image_reference=prepared.reference,
            app_port=app_port,
            needs_db=needs_db,
            labels=labels,
        )
        deploy_result = deploy_iteration(
            iteration_path,
            wait_timeout_s=wait_timeout_s,
            record_kind="probe",
            logger=logger,
        )
    except Exception as exc:
        logger.warning("deploy probe raised: %s", exc)
        probe = DeployProbeResult(
            ok=False,
            deploy_result=None,
            reason=str(exc),
            details=details,
        )
        return _finish_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            probe=probe,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
        )

    if not deploy_result.success:
        probe = DeployProbeResult(
            ok=False,
            deploy_result=deploy_result,
            reason="Kubernetes resources did not become Ready within timeout",
            details=details,
        )
        return _finish_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            probe=probe,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
        )

    endpoints_ok, endpoints_msg = check_service_endpoints_ready(
        namespace=spec.namespace,
        service="backend",
        logger=logger,
    )
    details["backend_endpoints"] = endpoints_msg
    if not endpoints_ok:
        probe = DeployProbeResult(
            ok=False,
            deploy_result=deploy_result,
            reason=endpoints_msg,
            details=details,
        )
        return _finish_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            probe=probe,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
        )

    profile = selected_cluster_profile(k8s_cluster)
    entry_node = profile.worker_nodes[0] if profile.worker_nodes else profile.control_node
    try:
        target = resolve_nodeport_target(
            namespace=spec.namespace,
            service="backend",
            service_port=spec.backend.port,
            node_host=entry_node,
            logger=logger,
        )
        details["nodeport_target"] = target
    except Exception as exc:
        probe = DeployProbeResult(
            ok=False,
            deploy_result=deploy_result,
            reason=f"NodePort resolution failed: {exc}",
            details=details,
        )
        return _finish_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            probe=probe,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
        )

    if spec.database.enabled:
        for svc in ("postgres", f"{spec.database.service_name}"):
            if svc == "backend":
                continue
            db_ok, db_msg = check_service_endpoints_ready(
                namespace=spec.namespace,
                service=svc,
                logger=logger,
            )
            details[f"{svc}_endpoints"] = db_msg
            if not db_ok and svc == spec.database.service_name:
                probe = DeployProbeResult(
                    ok=False,
                    deploy_result=deploy_result,
                    reason=db_msg,
                    details=details,
                )
                return _finish_deploy_attempt(
                    iteration_path=iteration_path,
                    iteration_id=iteration_id,
                    probe=probe,
                    attempt_index=attempt_index,
                    enable_attempts=enable_attempts,
                    persist_on_failure=persist_on_failure,
                    logger=logger,
                )

    logger.info("Deploy probe passed for %s", iteration_path)
    return DeployAttemptResult(ok=True, reason="deploy probe passed")


def _finish_deploy_attempt(
    *,
    iteration_path: Path,
    iteration_id: str,
    probe: DeployProbeResult,
    attempt_index: int,
    enable_attempts: bool,
    persist_on_failure: bool,
    logger: logging.Logger,
) -> DeployAttemptResult:
    failure = deploy_failure_record_from_probe(
        iteration_id, probe, attempt=attempt_index
    )
    if persist_on_failure:
        from ..failure.persist import persist_deploy_attempt_failure

        persist_deploy_attempt_failure(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            record=failure,
            logger=logger,
        )
    return DeployAttemptResult(ok=False, reason=probe.reason, failure=failure)


# ---------------------------------------------------------------------------
# Stage-level orchestration
# ---------------------------------------------------------------------------

def should_run_deploy_stage(plan: IterationPlan) -> bool:
    """
    Run deploy after spec when application code or deployment spec changed.

    Baseline is excluded: its spec retry loop probes inline via
    :func:`baseline_deploy_probe_callback`.
    """
    return plan.refinement_action in {"code", "deployment"}


def run_deploy_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
) -> DeployStageResult:
    """
    Bring the iteration up on the cluster so bench can run Locust immediately.

    Probes when ``refinement_action`` is ``code`` (new image, reused spec) or
    ``deployment`` (new spec). Skips baseline iterations that already probed
    during the spec retry loop.
    """
    from tasks import esc

    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )

    if not should_run_deploy_stage(plan):
        if probe_record_passed(iteration_path):
            logger.info(
                "iteration %s: deploy stage skipped (baseline probe already recorded)",
                plan.iteration_id,
            )
            return DeployStageResult(skipped=True)
        return DeployStageResult(
            reason="deploy probe required but baseline spec loop did not record success",
        )

    logger.info(
        "iteration %s: deploying for bench (action=%s, image=%s)",
        plan.iteration_id,
        plan.refinement_action,
        image_id,
    )
    sample_slug = (
        f"{esc(ctx.task.model)}-{esc(ctx.task.env.id)}-"
        f"{esc(ctx.task.scenario.id)}-sample{ctx.sample}"
    )
    labels = {
        "baxbench.dev/model": esc(ctx.task.model),
        "baxbench.dev/scenario": esc(ctx.task.scenario.id),
        "baxbench.dev/env": esc(ctx.task.env.id),
        "baxbench.dev/spec-gen": "true",
        "baxbench.dev/phase": str(plan.iteration_index),
    }
    result = run_deploy_attempt(
        iteration_path=iteration_path,
        iteration_id=plan.iteration_id,
        image_id=image_id,
        sample_slug=sample_slug,
        app_port=ctx.task.env.port,
        needs_db=ctx.task.scenario.needs_db,
        k8s_cluster=ctx.k8s_cluster,
        wait_timeout_s=cfg.k8s_wait_timeout,
        labels=labels,
        logger=logger,
        attempt_index=1,
        enable_attempts=False,
    )
    if result.ok:
        logger.info("deploy stage passed; cluster ready for bench")
        return DeployStageResult()

    logger.error("deploy stage failed: %s", result.reason)
    failure = result.failure
    iteration_failure = build_deploy_iteration_failure(
        iteration_path,
        iteration_id=plan.iteration_id,
        terminal_attempt=1,
        fallback=failure,
        logger=logger,
    )
    fail_iteration_phase(
        iteration_path=iteration_path,
        task_run_dir=ctx.task_run_dir,
        sample_dir=ctx.sample_dir,
        sample=ctx.sample,
        iteration_id=plan.iteration_id,
        kind="deploy",
        logger=logger,
        iteration_failure=iteration_failure,
    )
    return DeployStageResult(reason=result.reason)


# ---------------------------------------------------------------------------
# Baseline spec deploy-probe callback
# ---------------------------------------------------------------------------

def _bench_labels(ctx: SampleContext, plan: IterationPlan) -> dict[str, str]:
    from tasks import esc

    return {
        "baxbench.dev/model": esc(ctx.task.model),
        "baxbench.dev/scenario": esc(ctx.task.scenario.id),
        "baxbench.dev/env": esc(ctx.task.env.id),
        "baxbench.dev/spec-gen": "true",
        "baxbench.dev/phase": str(plan.iteration_index),
    }


def _sample_slug(ctx: SampleContext) -> str:
    from tasks import esc

    return (
        f"{esc(ctx.task.model)}-{esc(ctx.task.env.id)}-"
        f"{esc(ctx.task.scenario.id)}-sample{ctx.sample}"
    )


def baseline_deploy_probe_callback(
    ctx: SampleContext,
    plan: IterationPlan,
    image_id: str,
    cfg: RunConfig,
    iteration_path: Path,
    logger: logging.Logger,
) -> Callable[[], DeployAttemptResult]:
    """Zero-arg probe callable for the baseline spec retry loop."""

    def _probe() -> DeployAttemptResult:
        return run_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=plan.iteration_id,
            image_id=image_id,
            sample_slug=_sample_slug(ctx),
            app_port=ctx.task.env.port,
            needs_db=ctx.task.scenario.needs_db,
            k8s_cluster=ctx.k8s_cluster,
            wait_timeout_s=cfg.k8s_wait_timeout,
            labels=_bench_labels(ctx, plan),
            logger=logger,
            persist_on_failure=False,
        )

    return _probe


__all__ = [
    "DeployAttemptResult",
    "DeployProbeResult",
    "DeployStageResult",
    "baseline_deploy_probe_callback",
    "check_service_endpoints_ready",
    "deploy_failure_record_from_probe",
    "probe_record_passed",
    "rotate_top_level_into_attempt",
    "run_deploy_attempt",
    "run_deploy_stage",
    "should_run_deploy_stage",
]

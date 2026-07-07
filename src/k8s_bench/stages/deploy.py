"""
Deploy stage (``04-deploy/``): bring the iteration up on the cluster.

Patches ``spec.yaml`` with the bench image, renders manifests, applies them,
waits for readiness, and records ``04-deploy/probe.json``. Bench assumes this
stage succeeded before running Locust.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cluster.deploy import DeployResult, deploy_iteration
from ..cluster.images import prepare_image_for_k8s
from ..cluster.load_target import resolve_nodeport_target
from ..cluster.profiles import selected_cluster_profile
from ..failure import DeployFailureRecord, fail_iteration_phase
from ..failure.classify import classify_deploy_failure_kind
from ..failure.deploy_diagnostics import collect_deploy_failure_diagnostics
from ..failure.persist import build_deploy_iteration_failure
from ..iteration import ensure_iteration_spec
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..workspace import deploy_probe_record_path, iteration_deploy_dir, resolve_iteration_dir


# ---------------------------------------------------------------------------
# Result types (same pattern as :mod:`k8s_bench.stages.bench`)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeployAttemptResult:
    """Outcome of one deploy attempt."""

    ok: bool = False
    error: str = ""
    failure: DeployFailureRecord | None = None


@dataclass(frozen=True)
class DeployStageResult:
    """Set ``ok=True`` when the cluster is ready for bench."""

    ok: bool = False


# ---------------------------------------------------------------------------
# Attempt-level helpers
# ---------------------------------------------------------------------------

def _iteration_namespace(iteration_path: Path) -> str:
    try:
        from ..spec.models import K8sWorkloadSpec
        from ..workspace import find_iteration_spec_path

        spec_path = find_iteration_spec_path(iteration_path)
        if spec_path is None:
            return ""
        return K8sWorkloadSpec.from_yaml_file(spec_path).namespace
    except Exception:
        return ""


def build_deploy_failure_record(
    *,
    iteration_id: str,
    error: str,
    deploy_result: DeployResult | None = None,
    details: dict[str, Any] | None = None,
    attempt: int | None = None,
    namespace: str = "",
    logger: logging.Logger | None = None,
) -> DeployFailureRecord:
    merged_details = dict(details or {})
    if deploy_result is not None and deploy_result.wait_details:
        for resource, detail in deploy_result.wait_details.items():
            merged_details[f"wait/{resource}"] = str(detail)[:500]
    stdout = deploy_result.stdout if deploy_result is not None else ""
    stderr = deploy_result.stderr if deploy_result is not None else ""

    diagnostic_excerpt = ""
    if namespace.strip():
        try:
            diagnostic_excerpt = collect_deploy_failure_diagnostics(
                namespace,
                logger=logger,
            )
        except Exception as exc:
            if logger is not None:
                logger.warning("deploy diagnostics collection failed: %s", exc)

    kind = classify_deploy_failure_kind(
        error=error,
        reason=error,
        details=merged_details,
        diagnostic_excerpt=diagnostic_excerpt,
        stdout=stdout,
        stderr=stderr,
    )
    return DeployFailureRecord(
        phase="deploy",
        kind=kind,  # type: ignore[arg-type]
        iteration_id=iteration_id,
        attempt=attempt,
        summary=error,
        reason=error,
        details=merged_details,
        diagnostic_excerpt=diagnostic_excerpt,
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
    for sub in ("manifests",):
        src = phase_dir / sub
        if src.is_dir():
            dest = attempt_dir / sub
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))


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


def _fail_deploy_attempt(
    *,
    iteration_path: Path,
    iteration_id: str,
    error: str,
    deploy_result: DeployResult | None = None,
    details: dict[str, Any] | None = None,
    attempt_index: int,
    enable_attempts: bool,
    persist_on_failure: bool,
    logger: logging.Logger,
    namespace: str = "",
) -> DeployAttemptResult:
    ns = namespace or _iteration_namespace(iteration_path)
    failure = build_deploy_failure_record(
        iteration_id=iteration_id,
        error=error,
        deploy_result=deploy_result,
        details=details,
        attempt=attempt_index,
        namespace=ns,
        logger=logger,
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
    return DeployAttemptResult(ok=False, error=error, failure=failure)


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
    """Push image, render manifests, apply, and verify the iteration is reachable."""
    details: dict[str, Any] = {}
    deploy_result: DeployResult | None = None
    spec_namespace = ""

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
        spec_namespace = spec.namespace
        deploy_result = deploy_iteration(
            iteration_path,
            wait_timeout_s=wait_timeout_s,
            record_kind="probe",
            logger=logger,
        )
    except Exception as exc:
        logger.warning("deploy attempt raised: %s", exc)
        return _fail_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            error=str(exc),
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
            namespace=spec_namespace,
        )

    if not deploy_result.success:
        return _fail_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            error="Kubernetes resources did not become Ready within timeout",
            deploy_result=deploy_result,
            details=details,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
            namespace=spec_namespace,
        )

    endpoints_ok, endpoints_msg = check_service_endpoints_ready(
        namespace=spec.namespace,
        service="backend",
        logger=logger,
    )
    details["backend_endpoints"] = endpoints_msg
    if not endpoints_ok:
        return _fail_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            error=endpoints_msg,
            deploy_result=deploy_result,
            details=details,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
            namespace=spec.namespace,
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
        return _fail_deploy_attempt(
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            error=f"NodePort resolution failed: {exc}",
            deploy_result=deploy_result,
            details=details,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            persist_on_failure=persist_on_failure,
            logger=logger,
            namespace=spec.namespace,
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
                return _fail_deploy_attempt(
                    iteration_path=iteration_path,
                    iteration_id=iteration_id,
                    error=db_msg,
                    deploy_result=deploy_result,
                    details=details,
                    attempt_index=attempt_index,
                    enable_attempts=enable_attempts,
                    persist_on_failure=persist_on_failure,
                    logger=logger,
                    namespace=spec.namespace,
                )

    logger.info("Deploy passed for %s", iteration_path)
    return DeployAttemptResult(ok=True)


# ---------------------------------------------------------------------------
# Stage-level orchestration
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


def run_deploy_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    image_id: str,
    cfg: RunConfig,
    logger: logging.Logger,
) -> DeployStageResult:
    """
    Bring the iteration up on the cluster so bench can run Locust immediately.

    Always runs for every iteration (baseline and refinements).
    """
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )

    logger.info(
        "iteration %s: deploying (action=%s, image=%s)",
        plan.iteration_id,
        plan.refinement_action,
        image_id,
    )
    result = run_deploy_attempt(
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
        attempt_index=1,
        enable_attempts=False,
    )
    if result.ok:
        logger.info("deploy stage passed; cluster ready for bench")
        return DeployStageResult(ok=True)

    logger.error("deploy stage failed: %s", result.error)
    iteration_failure = build_deploy_iteration_failure(
        iteration_path,
        iteration_id=plan.iteration_id,
        terminal_attempt=1,
        fallback=result.failure,
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
    return DeployStageResult()


__all__ = [
    "DeployAttemptResult",
    "DeployStageResult",
    "build_deploy_failure_record",
    "check_service_endpoints_ready",
    "probe_record_passed",
    "rotate_top_level_into_attempt",
    "run_deploy_attempt",
    "run_deploy_stage",
]

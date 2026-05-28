"""Verify a rendered spec actually deploys and exposes a reachable backend Service."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cluster.deploy import DeployResult, deploy_iteration
from ..cluster.images import prepare_image_for_k8s
from ..cluster.load_target import resolve_nodeport_target
from ..cluster.profiles import selected_cluster_profile
from ..iteration import ensure_iteration_spec


@dataclass(frozen=True)
class DeployProbeResult:
    ok: bool
    deploy_result: DeployResult | None
    reason: str
    details: dict[str, Any]

    def to_prompt_feedback(self) -> str:
        lines = [
            "## Deploy probe failed (previous attempt)",
            "",
            self.reason,
        ]
        if self.deploy_result is not None and self.deploy_result.wait_details:
            lines.extend(["", "### kubectl wait details"])
            for resource, detail in self.deploy_result.wait_details.items():
                lines.append(f"- **{resource}**: {detail[:500]}")
        if self.details:
            lines.extend(["", "### Additional checks"])
            for key, value in self.details.items():
                lines.append(f"- **{key}**: {value}")
        lines.append("")
        lines.append(
            "Fix replicas, resources, and placement so pods schedule and become Ready."
        )
        return "\n".join(lines)


def _kubectl(args: list[str], *, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
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
    """
    Return whether the Service has at least one ready endpoint address.

    Does not send HTTP traffic — only inspects the Endpoints object via kubectl.
    """
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


def probe_iteration_deployable(
    *,
    iteration_path: Path,
    image_id: str,
    sample_slug: str,
    app_port: int,
    needs_db: bool,
    wait_timeout_s: int = 300,
    labels: dict[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> DeployProbeResult:
    """
    Render spec with the bench image, deploy to the cluster, and verify readiness.

    Checks:
    1. ``kubectl apply`` + deployment/statefulset Ready (via ``deploy_iteration``)
    2. Backend Service has ready Endpoints (no HTTP request)
    3. NodePort is configured and resolvable for external load generators
    """
    log = logger or logging.getLogger(__name__)
    details: dict[str, Any] = {}

    try:
        prepared = prepare_image_for_k8s(
            image_id,
            sample_slug=sample_slug,
            profile_name=selected_cluster_profile().name,
            logger=log,
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
            logger=log,
        )
    except Exception as exc:
        log.warning("deploy probe raised: %s", exc)
        return DeployProbeResult(
            ok=False,
            deploy_result=None,
            reason=str(exc),
            details=details,
        )

    if not deploy_result.success:
        return DeployProbeResult(
            ok=False,
            deploy_result=deploy_result,
            reason="Kubernetes resources did not become Ready within timeout",
            details=details,
        )

    endpoints_ok, endpoints_msg = check_service_endpoints_ready(
        namespace=spec.namespace,
        service="backend",
        logger=log,
    )
    details["backend_endpoints"] = endpoints_msg
    if not endpoints_ok:
        return DeployProbeResult(
            ok=False,
            deploy_result=deploy_result,
            reason=endpoints_msg,
            details=details,
        )

    profile = selected_cluster_profile()
    entry_node = profile.worker_nodes[0] if profile.worker_nodes else profile.control_node
    try:
        target = resolve_nodeport_target(
            namespace=spec.namespace,
            service="backend",
            service_port=spec.backend.port,
            node_host=entry_node,
            logger=log,
        )
        details["nodeport_target"] = target
    except Exception as exc:
        return DeployProbeResult(
            ok=False,
            deploy_result=deploy_result,
            reason=f"NodePort resolution failed: {exc}",
            details=details,
        )

    if spec.database.enabled:
        for svc in ("postgres", f"{spec.database.service_name}"):
            if svc == "backend":
                continue
            db_ok, db_msg = check_service_endpoints_ready(
                namespace=spec.namespace,
                service=svc,
                logger=log,
            )
            details[f"{svc}_endpoints"] = db_msg
            if not db_ok and svc == spec.database.service_name:
                return DeployProbeResult(
                    ok=False,
                    deploy_result=deploy_result,
                    reason=db_msg,
                    details=details,
                )

    log.info("Deploy probe passed for %s", iteration_path)
    return DeployProbeResult(
        ok=True,
        deploy_result=deploy_result,
        reason="deploy probe passed",
        details=details,
    )

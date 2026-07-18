from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..workspace.paths import (
    deploy_probe_record_path,
    iteration_manifests_dir,
    require_iteration_spec_path,
)


@dataclass(frozen=True)
class DeployResult:
    success: bool
    namespace: str
    manifest_file: str
    kubectl_context: str | None
    backend_service_url: str
    applied_at: str
    stdout: str
    stderr: str
    wait_details: dict[str, str]
    # Runtime snapshot (filled after a successful deploy probe; not in LLM spec.yaml).
    image_reference: str = ""
    backend_port: int = 0
    nodeport_target: str = ""
    deploy_labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def with_runtime(
        self,
        *,
        image_reference: str,
        backend_port: int,
        nodeport_target: str,
        deploy_labels: dict[str, str] | None = None,
    ) -> DeployResult:
        return replace(
            self,
            image_reference=image_reference,
            backend_port=backend_port,
            nodeport_target=nodeport_target,
            deploy_labels=dict(deploy_labels or {}),
        )


def _kubectl(
    args: Sequence[str],
    *,
    timeout_s: int | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["kubectl", *args]
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def current_context() -> str | None:
    proc = _kubectl(["config", "current-context"])
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def apply_manifests(
    manifest_file: Path,
    *,
    namespace: str,
    wait_timeout_s: int = 300,
    wait_deployments: tuple[str, ...] = ("deployment/postgres", "deployment/backend"),
    wait_statefulsets: tuple[str, ...] = (),
    statefulset_wait_timeout_s: int | None = None,
    logger: logging.Logger | None = None,
) -> DeployResult:
    log = logger or logging.getLogger(__name__)
    manifest_file = manifest_file.resolve()
    if not manifest_file.is_file():
        raise FileNotFoundError(manifest_file)

    apply_proc = _kubectl(["apply", "-f", str(manifest_file)])
    log.info("kubectl apply -f %s (rc=%s)", manifest_file, apply_proc.returncode)
    if apply_proc.stdout:
        log.info(apply_proc.stdout.strip())
    if apply_proc.stderr:
        log.info(apply_proc.stderr.strip())

    wait_details: dict[str, str] = {}
    success = apply_proc.returncode == 0
    if success:
        for resource in wait_deployments:
            wait_proc = _kubectl(
                [
                    "wait",
                    "--for=condition=available",
                    resource,
                    "-n",
                    namespace,
                    f"--timeout={wait_timeout_s}s",
                ],
                timeout_s=wait_timeout_s + 30,
            )
            wait_details[resource] = (wait_proc.stdout or wait_proc.stderr or "").strip()
            if wait_proc.returncode != 0:
                if resource == "deployment/postgres" and "NotFound" in wait_details[resource]:
                    wait_details[resource] = "skipped (not found)"
                    continue
                success = False
                log.warning("wait failed for %s: %s", resource, wait_details[resource])
                if resource == "deployment/backend":
                    diag = _kubectl(
                        [
                            "get",
                            "pods",
                            "-n",
                            namespace,
                            "-l",
                            "app=backend",
                            "-o",
                            "wide",
                        ],
                        timeout_s=30,
                    )
                    if diag.stdout:
                        log.warning("backend pods:\n%s", diag.stdout.strip())
                    ev = _kubectl(
                        [
                            "describe",
                            "pod",
                            "-n",
                            namespace,
                            "-l",
                            "app=backend",
                        ],
                        timeout_s=60,
                    )
                    if ev.stdout:
                        tail = "\n".join((ev.stdout or "").splitlines()[-25:])
                        log.warning("backend pod events (tail):\n%s", tail)
                    logs = _kubectl(
                        [
                            "logs",
                            "-n",
                            namespace,
                            "-l",
                            "app=backend",
                            "--tail=40",
                            "--prefix=true",
                        ],
                        timeout_s=30,
                    )
                    if logs.stdout:
                        log.warning("backend pod logs (tail):\n%s", logs.stdout.strip())
                    elif logs.stderr:
                        log.warning("backend pod logs unavailable: %s", logs.stderr.strip())
                elif resource.startswith("deployment/") and (
                    resource.endswith("/redis") or resource.endswith("redis-db")
                ):
                    app_label = resource.split("/", 1)[1]
                    diag = _kubectl(
                        [
                            "get",
                            "pods",
                            "-n",
                            namespace,
                            "-l",
                            f"app={app_label}",
                            "-o",
                            "wide",
                        ],
                        timeout_s=30,
                    )
                    if diag.stdout:
                        log.warning("redis pods:\n%s", diag.stdout.strip())
                    logs = _kubectl(
                        [
                            "logs",
                            "-n",
                            namespace,
                            "-l",
                            f"app={app_label}",
                            "--tail=40",
                            "--prefix=true",
                        ],
                        timeout_s=30,
                    )
                    if logs.stdout:
                        log.warning("redis pod logs (tail):\n%s", logs.stdout.strip())
                    elif logs.stderr:
                        log.warning("redis pod logs unavailable: %s", logs.stderr.strip())

        ss_timeout = statefulset_wait_timeout_s or wait_timeout_s
        for resource in wait_statefulsets:
            # ``kubectl wait`` does not understand a StatefulSet rollout
            # directly; use the rollout subcommand which polls
            # ``.status.readyReplicas == .spec.replicas``.
            wait_proc = _kubectl(
                [
                    "rollout",
                    "status",
                    resource,
                    "-n",
                    namespace,
                    f"--timeout={ss_timeout}s",
                ],
                timeout_s=ss_timeout + 30,
            )
            wait_details[resource] = (wait_proc.stdout or wait_proc.stderr or "").strip()
            if wait_proc.returncode != 0:
                if "NotFound" in wait_details[resource]:
                    wait_details[resource] = "skipped (not found)"
                    continue
                success = False
                log.warning("wait failed for %s: %s", resource, wait_details[resource])
                diag = _kubectl(
                    ["get", "pods", "-n", namespace, "-l", "baxbench.dev/db-tier=replica", "-o", "wide"],
                    timeout_s=30,
                )
                if diag.stdout:
                    log.warning("replica pods:\n%s", diag.stdout.strip())
                ev = _kubectl(
                    ["describe", "pod", "-n", namespace, "-l", "baxbench.dev/db-tier=replica"],
                    timeout_s=60,
                )
                if ev.stdout:
                    tail = "\n".join((ev.stdout or "").splitlines()[-25:])
                    log.warning("replica events (tail):\n%s", tail)
                rlogs = _kubectl(
                    ["logs", "-n", namespace, "-l", "baxbench.dev/db-tier=replica", "--tail=40", "--prefix=true"],
                    timeout_s=30,
                )
                if rlogs.stdout:
                    log.warning("replica logs (tail):\n%s", rlogs.stdout.strip())
                elif rlogs.stderr:
                    log.warning("replica logs unavailable: %s", rlogs.stderr.strip())

    backend_host = f"backend.{namespace}.svc.cluster.local"
    return DeployResult(
        success=success,
        namespace=namespace,
        manifest_file=str(manifest_file),
        kubectl_context=current_context(),
        backend_service_url=f"http://{backend_host}",
        applied_at=datetime.now(timezone.utc).isoformat(),
        stdout=apply_proc.stdout or "",
        stderr=apply_proc.stderr or "",
        wait_details=wait_details,
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


def write_deploy_record(iteration_path: Path, result: DeployResult) -> Path:
    """Persist deploy outcome to ``04-deploy/probe.json``."""
    path = deploy_probe_record_path(iteration_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def delete_iteration_namespace(namespace: str, *, logger: logging.Logger | None = None) -> None:
    log = logger or logging.getLogger(__name__)
    proc = _kubectl(["delete", "namespace", namespace, "--wait=true", "--timeout=120s"], timeout_s=150)
    log.info("kubectl delete namespace %s (rc=%s)", namespace, proc.returncode)


def deploy_iteration(
    iteration_path: Path,
    *,
    spec: "K8sWorkloadSpec | None" = None,
    wait_timeout_s: int = 300,
    write_record: bool = True,
    logger: logging.Logger | None = None,
) -> DeployResult:
    from ..spec.models import K8sWorkloadSpec
    from ..spec.render import write_manifest_files
    from .cleanup import cleanup_baxbench_namespaces_before_deploy

    log = logger or logging.getLogger(__name__)
    cleanup_baxbench_namespaces_before_deploy(logger=log)

    if spec is None:
        spec_path = require_iteration_spec_path(iteration_path)
        spec = K8sWorkloadSpec.from_yaml_file(spec_path)
    manifest_file = write_manifest_files(spec, iteration_manifests_dir(iteration_path))
    waits: list[str] = ["deployment/backend"]
    statefulset_waits: list[str] = []
    statefulset_wait_timeout_s = wait_timeout_s
    if spec.database.enabled:
        waits.insert(0, "deployment/postgres")
        if spec.pooler.enabled:
            # Backends connect via the pooler; wait for it after postgres.
            waits.insert(1, f"deployment/{spec.pooler.service_name}")
        if spec.read_pooler.enabled and spec.database.replicas > 1:
            waits.append(f"deployment/{spec.read_pooler.service_name}")
        if spec.cache.enabled:
            waits.append(f"deployment/{spec.cache.service_name}")
        db_cache = spec.database.cache
        if db_cache.enabled and not db_cache.use_shared:
            waits.append(f"deployment/{db_cache.service_name}")
        if spec.database.replicas > 1:
            statefulset_waits.append(
                f"statefulset/{spec.database.service_name}-replica"
            )
            # Replicas pg_basebackup from the primary on first start. Allow at
            # least 5 min wall-clock (per replica's readiness probe window),
            # or wait_timeout_s if the caller asked for more.
            statefulset_wait_timeout_s = max(wait_timeout_s, 300)
    result = apply_manifests(
        manifest_file,
        namespace=spec.namespace,
        wait_timeout_s=wait_timeout_s,
        wait_deployments=tuple(waits),
        wait_statefulsets=tuple(statefulset_waits),
        statefulset_wait_timeout_s=statefulset_wait_timeout_s,
        logger=log,
    )
    if write_record:
        write_deploy_record(iteration_path, result)
    return result


def render_and_deploy(
    iteration_path: Path,
    *,
    wait_timeout_s: int = 300,
    logger: logging.Logger | None = None,
) -> DeployResult:
    result = deploy_iteration(iteration_path, wait_timeout_s=wait_timeout_s, logger=logger)
    if not result.success:
        raise RuntimeError(f"K8s deploy failed for {iteration_path}; see 04-deploy/probe.json")
    return result

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .paths import deploy_record_path, iteration_manifests_dir


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

    def to_dict(self) -> dict:
        return asdict(self)


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
                # postgres may be absent when DB disabled
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


def write_deploy_record(iteration_path: Path, result: DeployResult) -> Path:
    path = deploy_record_path(iteration_path)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def delete_iteration_namespace(namespace: str, *, logger: logging.Logger | None = None) -> None:
    log = logger or logging.getLogger(__name__)
    proc = _kubectl(["delete", "namespace", namespace, "--wait=true", "--timeout=120s"], timeout_s=150)
    log.info("kubectl delete namespace %s (rc=%s)", namespace, proc.returncode)


def deploy_iteration(
    iteration_path: Path,
    *,
    wait_timeout_s: int = 300,
    logger: logging.Logger | None = None,
) -> DeployResult:
    from .models import K8sWorkloadSpec

    spec = K8sWorkloadSpec.from_yaml_file(iteration_path / "spec.yaml")
    manifest_file = iteration_manifests_dir(iteration_path) / "all.yaml"
    if not manifest_file.is_file():
        raise FileNotFoundError(
            f"Missing {manifest_file}; run render first (k8s_bench render <iteration_path>)."
        )
    waits: list[str] = ["deployment/backend"]
    if spec.database.enabled:
        waits.insert(0, "deployment/postgres")
    result = apply_manifests(
        manifest_file,
        namespace=spec.namespace,
        wait_timeout_s=wait_timeout_s,
        wait_deployments=tuple(waits),
        logger=logger,
    )
    write_deploy_record(iteration_path, result)
    return result

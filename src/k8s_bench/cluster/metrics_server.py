"""Install and verify metrics-server for ``kubectl top`` utilization sampling."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Sequence

_METRICS_SERVER_MANIFEST = (
    "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
)
_INSECURE_TLS_ARG = "--kubelet-insecure-tls"


def _kubectl_env(kubeconfig: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    if kubeconfig is not None:
        env["KUBECONFIG"] = str(kubeconfig)
    return env


def _kubectl(
    args: Sequence[str],
    *,
    kubeconfig: Path | None = None,
    timeout_s: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        env=_kubectl_env(kubeconfig),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def metrics_api_available(*, kubeconfig: Path | None = None, timeout_s: int = 30) -> bool:
    """True when ``kubectl top nodes`` returns at least one row."""
    proc = _kubectl(
        ("top", "nodes", "--no-headers"),
        kubeconfig=kubeconfig,
        timeout_s=timeout_s,
    )
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


def warn_if_metrics_api_unavailable(
    logger: logging.Logger,
    *,
    kubeconfig: Path | None = None,
) -> None:
    if metrics_api_available(kubeconfig=kubeconfig):
        logger.info("Metrics API OK (kubectl top nodes)")
        return
    logger.warning(
        "Metrics API not available — perf runs will have empty diagnostics/kubernetes/cluster/kubectl_top_*.csv. "
        "Install metrics-server: re-run ./scripts/k8s_setup_cluster.sh (idempotent), or "
        "kubectl apply -f %s && patch metrics-server with %s for kubeadm lab clusters.",
        _METRICS_SERVER_MANIFEST,
        _INSECURE_TLS_ARG,
    )


def _deployment_exists(kubeconfig: Path) -> bool:
    proc = _kubectl(
        ("get", "deployment", "-n", "kube-system", "metrics-server"),
        kubeconfig=kubeconfig,
    )
    return proc.returncode == 0


def _container_args(kubeconfig: Path) -> list[str]:
    proc = _kubectl(
        (
            "get",
            "deployment",
            "-n",
            "kube-system",
            "metrics-server",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].args}",
        ),
        kubeconfig=kubeconfig,
    )
    if proc.returncode != 0:
        return []
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return list(parsed) if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _patch_kubelet_insecure_tls(kubeconfig: Path, logger: logging.Logger) -> None:
    args = _container_args(kubeconfig)
    if _INSECURE_TLS_ARG in args:
        logger.info("metrics-server already has %s", _INSECURE_TLS_ARG)
        return
    logger.info("Patching metrics-server with %s (common for kubeadm lab clusters)", _INSECURE_TLS_ARG)
    patch = _kubectl(
        (
            "patch",
            "deployment",
            "metrics-server",
            "-n",
            "kube-system",
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "add",
                        "path": "/spec/template/spec/containers/0/args/-",
                        "value": _INSECURE_TLS_ARG,
                    }
                ]
            ),
        ),
        kubeconfig=kubeconfig,
        timeout_s=90,
    )
    if patch.returncode != 0:
        raise RuntimeError(
            "metrics-server patch failed:\n"
            f"{(patch.stderr or patch.stdout or '').strip()}"
        )


def _wait_metrics_api(
    kubeconfig: Path,
    logger: logging.Logger,
    *,
    timeout_s: int = 180,
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if metrics_api_available(kubeconfig=kubeconfig, timeout_s=45):
            sample = _kubectl(
                ("top", "nodes", "--no-headers"),
                kubeconfig=kubeconfig,
                timeout_s=45,
            )
            logger.info("metrics-server ready:\n%s", (sample.stdout or "").strip())
            return
        time.sleep(5)
    raise RuntimeError(
        f"Timed out after {timeout_s}s waiting for Metrics API (kubectl top nodes). "
        "Check: kubectl -n kube-system logs -l k8s-app=metrics-server"
    )


def install_metrics_server(
    kubeconfig: Path,
    logger: logging.Logger,
    *,
    wait_timeout_s: int = 180,
) -> None:
    """Apply upstream metrics-server and wait until ``kubectl top`` works."""
    if _deployment_exists(kubeconfig):
        logger.info("metrics-server deployment already present")
    else:
        logger.info("Installing metrics-server (%s)", _METRICS_SERVER_MANIFEST)
        apply = _kubectl(
            ("apply", "-f", _METRICS_SERVER_MANIFEST),
            kubeconfig=kubeconfig,
            timeout_s=120,
        )
        if apply.returncode != 0:
            raise RuntimeError(
                "metrics-server apply failed:\n"
                f"{(apply.stderr or apply.stdout or '').strip()}"
            )
    _patch_kubelet_insecure_tls(kubeconfig, logger)
    _wait_metrics_api(kubeconfig, logger, timeout_s=wait_timeout_s)

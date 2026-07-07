"""Heuristic failure-kind classification for deploy and bench phases."""

from __future__ import annotations

from typing import Any

DeployFailureKind = str
BenchFailureKind = str

_DEPLOY_KINDS = frozenset(
    {
        "image_pull",
        "namespace_cleanup",
        "unschedulable",
        "crashloop",
        "oomkilled",
        "readiness_probe",
        "endpoints_unavailable",
        "kubectl_apply",
        "timeout",
        "unknown",
    }
)

_BENCH_KINDS = frozenset(
    {
        "locust_infra",
        "target_unreachable",
        "timeout_or_stall",
        "unknown",
    }
)


def classify_deploy_failure_kind(
    *,
    error: str = "",
    reason: str = "",
    details: dict[str, Any] | None = None,
    diagnostic_excerpt: str = "",
    stdout: str = "",
    stderr: str = "",
) -> DeployFailureKind:
    """Coarse deploy root-cause bucket from error text and kubectl evidence."""
    parts: list[str] = [error, reason, diagnostic_excerpt, stdout, stderr]
    if details:
        for key, value in details.items():
            parts.append(str(key))
            parts.append(str(value))
    blob = "\n".join(parts).lower()
    if not blob.strip():
        return "unknown"

    if any(
        s in blob
        for s in (
            "errimagepull",
            "imagepullbackoff",
            "image pull",
            "pull access denied",
            "manifest unknown",
            "failed to pull image",
        )
    ):
        return "image_pull"
    if any(
        s in blob
        for s in (
            "namespace",
            "already exists",
            "terminating",
            "stuck",
            "cleanup",
        )
    ) and any(s in blob for s in ("delete", "conflict", "terminat")):
        return "namespace_cleanup"
    if any(
        s in blob
        for s in (
            "0/",
            "nodes are available",
            "unschedulable",
            "insufficient cpu",
            "insufficient memory",
            "didn't match node selector",
            "didn't tolerate",
            "no nodes",
        )
    ):
        return "unschedulable"
    if "crashloopbackoff" in blob or "crash loop" in blob:
        return "crashloop"
    if "oomkilled" in blob or "out of memory" in blob:
        return "oomkilled"
    if "endpoints/" in blob or "no ready addresses" in blob or "endpoints unavailable" in blob:
        return "endpoints_unavailable"
    if any(s in blob for s in ("readiness probe", "probe failed", "not ready")):
        return "readiness_probe"
    if any(s in blob for s in ("kubectl apply", "error from server", "invalid")) and (
        "apply" in blob or "from server" in blob
    ):
        return "kubectl_apply"
    if any(
        s in blob
        for s in (
            "did not become ready",
            "condition met",
            "within timeout",
            "timed out",
            "timeout",
            "deadline exceeded",
        )
    ):
        return "timeout"
    return "unknown"


def classify_bench_failure_kind(text: str) -> BenchFailureKind:
    """Coarse bench root-cause bucket from harness error text."""
    blob = (text or "").lower()
    if not blob.strip():
        return "unknown"

    if any(
        s in blob
        for s in (
            "locust",
            "worker",
            "master",
            "ramping",
            "loadgen",
            "load generator",
            "greenlet",
            "gevent",
        )
    ) and any(
        s in blob
        for s in (
            "exception",
            "traceback",
            "failed",
            "disconnect",
            "stopped",
            "could not connect",
            "no workers",
        )
    ):
        return "locust_infra"

    if any(
        s in blob
        for s in (
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname provided",
            "could not resolve host",
            "dns",
            "connection refused",
            "no route to host",
            "network is unreachable",
            "connection timed out",
            "read timed out",
            "connect timeout",
            "failed to establish a new connection",
            "max retries exceeded",
            "bad gateway",
            "service unavailable",
        )
    ):
        return "target_unreachable"

    if any(s in blob for s in ("timeout", "timed out", "stalled", "stall", "hung")):
        return "timeout_or_stall"

    return "unknown"


def normalize_deploy_failure_kind(raw: str) -> DeployFailureKind:
    kind = (raw or "unknown").strip().lower()
    if kind in {"deploy_probe", "rbac_denied"}:
        return "unknown"
    return kind if kind in _DEPLOY_KINDS else "unknown"


def normalize_bench_failure_kind(raw: str, *, legacy_reason_kind: str = "") -> BenchFailureKind:
    kind = (raw or legacy_reason_kind or "unknown").strip().lower()
    if kind == "bench_run":
        legacy = (legacy_reason_kind or "unknown").strip().lower()
        return legacy if legacy in _BENCH_KINDS else "unknown"
    return kind if kind in _BENCH_KINDS else "unknown"

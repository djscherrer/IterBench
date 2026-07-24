"""Collect kubectl pod snapshots for deploy failure triage."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from ..failure.text import trim

_LOG_PRIORITY_PODS = ("backend", "postgres", "pgbouncer")
_BAD_PHASES = frozenset(
    {
        "Failed",
        "Unknown",
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
    }
)


def _kubectl(
    args: list[str], *, timeout_s: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _pod_score(pod: dict[str, Any]) -> int:
    status = pod.get("status") or {}
    phase = str(status.get("phase") or "")
    score = 0
    if phase in {"Pending", "Failed", "Unknown"}:
        score += 20
    for cs in status.get("containerStatuses") or []:
        state = cs.get("state") or {}
        waiting = state.get("waiting") or {}
        reason = str(waiting.get("reason") or "")
        if reason in _BAD_PHASES:
            score += 50
        if not cs.get("ready"):
            score += 10
        terminated = state.get("terminated") or {}
        if str(terminated.get("reason") or "") == "OOMKilled":
            score += 40
    return score


def _pick_worst_pod(pods: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pods:
        return None
    return max(pods, key=lambda p: (_pod_score(p), p.get("metadata", {}).get("name", "")))


def _ordered_pod_names(pods: list[dict[str, Any]]) -> list[str]:
    by_name = {
        str((p.get("metadata") or {}).get("name") or ""): p for p in pods if p.get("metadata")
    }
    ordered: list[str] = []
    for preferred in _LOG_PRIORITY_PODS:
        for name in sorted(by_name):
            if name.startswith(preferred) and name not in ordered:
                ordered.append(name)
    worst = _pick_worst_pod(pods)
    if worst is not None:
        worst_name = str((worst.get("metadata") or {}).get("name") or "")
        if worst_name and worst_name not in ordered:
            ordered.insert(0, worst_name)
    for name in sorted(by_name):
        if name not in ordered:
            ordered.append(name)
    return ordered[:3]


def collect_deploy_failure_diagnostics(
    namespace: str,
    *,
    logger: logging.Logger | None = None,
    log_tail_lines: int = 80,
    max_chars: int = 8000,
) -> str:
    """
    Snapshot pods + describe + log tails for a failed deploy namespace.

    Returns a trimmed text block suitable for ``DeployFailureRecord.diagnostic_excerpt``.
    """
    log = logger or logging.getLogger(__name__)
    ns = (namespace or "").strip()
    if not ns:
        return ""

    sections: list[str] = []

    pods_proc = _kubectl(["get", "pods", "-n", ns, "-o", "wide"], timeout_s=45)
    if pods_proc.stdout.strip():
        sections.append("### kubectl get pods -o wide\n```\n" + pods_proc.stdout.strip() + "\n```")
    elif pods_proc.stderr.strip():
        sections.append("### kubectl get pods (error)\n```\n" + pods_proc.stderr.strip() + "\n```")

    pods: list[dict[str, Any]] = []
    pods_json = _kubectl(["get", "pods", "-n", ns, "-o", "json"], timeout_s=45)
    if pods_json.returncode == 0 and pods_json.stdout.strip():
        try:
            data = json.loads(pods_json.stdout)
            pods = [p for p in data.get("items") or [] if isinstance(p, dict)]
        except json.JSONDecodeError:
            log.debug("could not parse pods json for namespace %s", ns)

    for pod_name in _ordered_pod_names(pods):
        describe = _kubectl(["describe", "pod", pod_name, "-n", ns], timeout_s=45)
        body = (describe.stdout or describe.stderr or "").strip()
        if body:
            sections.append(
                f"### kubectl describe pod {pod_name}\n```\n"
                + trim(body, max_chars=2500)
                + "\n```"
            )

        logs = _kubectl(
            ["logs", pod_name, "-n", ns, f"--tail={log_tail_lines}"],
            timeout_s=45,
        )
        log_body = (logs.stdout or logs.stderr or "").strip()
        if log_body:
            sections.append(
                f"### kubectl logs {pod_name} --tail={log_tail_lines}\n```\n"
                + trim(log_body, max_chars=1500)
                + "\n```"
            )

    return trim("\n\n".join(sections), max_chars=max_chars)

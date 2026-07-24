"""
Reclaim disk before k8s experiments by pruning unused container images.

Runs on every Kubernetes SSH host (control-plane + workers):

- ``docker image prune -af`` when Docker is present (build host / legacy loads)
- ``crictl rmi --prune`` when containerd/CRI is present (kubelet image store)
- wipe/recreate the local ``baxbench-registry`` volume (control-plane)

Resume may need a short ``docker build`` + push; that is intentional for lab disk.
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

from .preflight import (
    _dedupe_hosts,
    _run_shell_on_host,
    apply_cluster_profile_to_env,
)
from .profiles import resolve_cluster_profile, selected_cluster_profile
from .registry import wipe_local_registry


# Explicit CRI endpoints avoid crictl falling back to the removed dockershim sock.
_CRICTL = (
    "crictl "
    "--runtime-endpoint unix:///run/containerd/containerd.sock "
    "--image-endpoint unix:///run/containerd/containerd.sock"
)

_PRUNE_SCRIPT = rf"""
set +e
echo "=== image prune on $(hostname -s) ==="
if command -v docker >/dev/null 2>&1; then
  echo "-- docker image prune -af --"
  docker image prune -af 2>&1 | tail -n 8
else
  echo "-- docker: not installed (skip) --"
fi
if command -v crictl >/dev/null 2>&1; then
  echo "-- crictl rmi --prune --"
  if command -v sudo >/dev/null 2>&1; then
    sudo -n {_CRICTL} rmi --prune 2>&1 | tail -n 20
  else
    {_CRICTL} rmi --prune 2>&1 | tail -n 20
  fi
else
  echo "-- crictl: not installed (skip) --"
fi
df -h / | tail -n 1
echo "=== prune done ==="
"""


def image_prune_enabled() -> bool:
    raw = os.environ.get("BAXBENCH_K8S_IMAGE_PRUNE", "true").strip().lower()
    return raw not in ("0", "false", "no", "off", "none")


def prune_unused_images_on_hosts(
    hosts: Sequence[str],
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Prune unused Docker + containerd images on each host (best-effort)."""
    log = logger or logging.getLogger(__name__)
    targets = _dedupe_hosts(hosts)
    if not targets:
        log.info("No hosts for image prune")
        return
    log.info(
        "Pruning unused container images on %d host(s): %s",
        len(targets),
        ", ".join(targets),
    )
    for host in targets:
        try:
            proc = _run_shell_on_host(host, _PRUNE_SCRIPT, log)
            out = (proc.stdout or b"").decode(errors="replace")
            err = (proc.stderr or b"").decode(errors="replace")
            combined = (out + "\n" + err).strip()
            for line in combined.splitlines()[-8:]:
                if line.strip():
                    log.info("[%s] %s", host, line.strip()[:240])
            if proc.returncode != 0:
                log.warning(
                    "Image prune on %s exited %s (continuing)",
                    host,
                    proc.returncode,
                )
        except Exception as exc:  # noqa: BLE001 — never block the experiment
            log.warning("Image prune on %s failed: %s (continuing)", host, exc)


def prune_unused_images_for_cluster(
    profile_name: str | None = None,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """
    Prune unused images on the selected cluster's Kubernetes nodes.

    Covers control-plane and workers (``K8sClusterProfile.k8s_ssh_hosts``).
    Locust-only hosts are skipped — they do not pull workload images.
    """
    log = logger or logging.getLogger(__name__)
    if not image_prune_enabled():
        log.info("Skipping image prune (BAXBENCH_K8S_IMAGE_PRUNE disabled)")
        return
    if profile_name:
        apply_cluster_profile_to_env(profile_name)
        profile = resolve_cluster_profile(profile_name)
    else:
        profile = selected_cluster_profile()
    hosts = profile.k8s_ssh_hosts
    if not hosts:
        log.warning(
            "Cluster profile %s has no k8s_ssh_hosts; skipping image prune",
            profile.name,
        )
        return
    prune_unused_images_on_hosts(hosts, logger=log)
    try:
        wipe_local_registry(profile_name=profile.name, logger=log)
    except Exception as exc:  # noqa: BLE001 — never block the experiment
        log.warning("Registry wipe failed: %s (continuing)", exc)

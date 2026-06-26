from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Sequence

from .preflight import _dedupe_hosts, _is_local_host
from .registry import push_image_to_registry, resolve_registry_config


@dataclass(frozen=True)
class PreparedImage:
    reference: str


def _slug_ref(text: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-")
    return slug[:96] or "sample"


def resolve_image_worker_hosts(profile_name: str) -> tuple[str, ...]:
    """SSH hosts for legacy docker-save distribution (profile worker_nodes)."""
    from .profiles import resolve_cluster_profile

    return resolve_cluster_profile(profile_name).worker_nodes


def distribute_image_to_hosts(
    image_ref: str,
    hosts: Sequence[str],
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Legacy: docker save | ssh docker load (use registry instead when possible)."""
    log = logger or logging.getLogger(__name__)
    targets = [h for h in _dedupe_hosts(hosts) if not _is_local_host(h)]
    if not targets:
        return

    log.info(
        "Distributing image to %d worker(s) (docker save | ssh docker load): %s",
        len(targets),
        image_ref,
    )
    for i, host in enumerate(targets, start=1):
        log.info("[%d/%d] Loading image on %s", i, len(targets), host)
        save = subprocess.Popen(
            ["docker", "save", image_ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert save.stdout is not None
        load_script = (
            "set -euo pipefail; "
            "if command -v sudo >/dev/null 2>&1; then "
            "sudo -n docker load 2>/dev/null || docker load; "
            "else docker load; fi"
        )
        load = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, f"bash -lc {shlex.quote(load_script)}"],
            stdin=save.stdout,
            capture_output=True,
            check=False,
        )
        save.wait(timeout=600)
        if save.returncode != 0:
            err = (save.stderr or b"").decode(errors="ignore")
            raise RuntimeError(f"docker save {image_ref} failed: {err}")
        if load.returncode != 0:
            out = (load.stdout or b"").decode(errors="ignore") + (
                load.stderr or b""
            ).decode(errors="ignore")
            raise RuntimeError(f"docker load on {host} failed:\n{out.strip()}")
        log.info("[%d/%d] %s — OK", i, len(targets), host)


def expected_registry_reference(
    image_id: str,
    *,
    sample_slug: str,
    profile_name: str | None = None,
) -> str | None:
    """
    Compute the registry reference :func:`prepare_image_for_k8s` would push to.

    Pure / side-effect-free: lets callers cheaply check "would a probe-time push
    have produced *this* tag?" without re-pushing. Returns
    ``None`` when no registry is configured (legacy SSH-load path).
    """
    profile = (profile_name or "").strip() or None
    registry = resolve_registry_config(profile)
    if registry is None:
        return None
    short = image_id.removeprefix("sha256:")[:12]
    repo = _slug_ref(sample_slug)
    return f"{registry.endpoint}/baxbench/{repo}:{short}"


def prepare_image_for_k8s(
    image_id: str,
    *,
    sample_slug: str,
    worker_hosts: Sequence[str] | None = None,
    distribute_to_workers: bool = True,
    profile_name: str | None = None,
    logger: logging.Logger | None = None,
) -> PreparedImage:
    """
    Prepare image for Kubernetes: push to lab registry (preferred) or SSH-load to workers.
    """
    log = logger or logging.getLogger(__name__)
    short = image_id.removeprefix("sha256:")[:12]
    repo = _slug_ref(sample_slug)
    profile = (profile_name or "").strip() or None

    registry = resolve_registry_config(profile, logger=log)
    if registry is not None:
        reference = push_image_to_registry(
            image_id,
            repository=repo,
            tag=short,
            profile_name=profile,
            logger=log,
        )
        log.info("Image in registry: %s", reference)
        return PreparedImage(reference=reference)

    reference = f"baxbench-local/{repo}:{short}"
    tag_proc = subprocess.run(
        ["docker", "tag", image_id, reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if tag_proc.returncode != 0:
        raise RuntimeError(
            f"docker tag {image_id} {reference} failed: {(tag_proc.stderr or tag_proc.stdout).strip()}"
        )
    log.info("Tagged image for Kubernetes: %s", reference)

    if distribute_to_workers:
        hosts = tuple(worker_hosts or ())
        if not hosts and profile:
            hosts = resolve_image_worker_hosts(profile)
        if hosts:
            distribute_image_to_hosts(reference, hosts, logger=log)
        else:
            log.warning(
                "No registry and profile '%s' has no worker_nodes — image only on this machine. "
                "Run ./scripts/k8s_setup_cluster.sh or enable registry in the profile.",
                profile or "?",
            )

    return PreparedImage(reference=reference)

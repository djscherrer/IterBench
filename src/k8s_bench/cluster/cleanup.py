"""Remove BaxBench iteration namespaces from the cluster."""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable

from .deploy import _kubectl, delete_iteration_namespace

BAXBENCH_NAMESPACE_PREFIX = "baxbench-"


def resolve_k8s_cleanup_mode() -> str:
    """
    ``BAXBENCH_K8S_CLEANUP`` controls automatic namespace removal.

    - ``before`` — delete all ``baxbench-*`` namespaces before each deploy
    - ``after`` — delete all ``baxbench-*`` namespaces after a bench task finishes
    - ``both`` — before each deploy and after the run (default)
    - ``false`` / ``off`` — no automatic cleanup
    """
    raw = os.environ.get("BAXBENCH_K8S_CLEANUP", "both").strip().lower()
    if raw in ("0", "false", "no", "off", "none", ""):
        return "false"
    if raw in ("true", "yes", "1", "on"):
        return "both"
    if raw in ("before", "after", "both"):
        return raw
    return "both"


def cleanup_before_deploy_enabled() -> bool:
    mode = resolve_k8s_cleanup_mode()
    return mode in ("before", "both")


def cleanup_after_bench_enabled() -> bool:
    mode = resolve_k8s_cleanup_mode()
    return mode in ("after", "both")


def list_baxbench_namespaces() -> list[str]:
    proc = _kubectl(["get", "namespace", "-o", "json"], timeout_s=60)
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []
    names: list[str] = []
    for item in data.get("items") or []:
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "")
        if name.startswith(BAXBENCH_NAMESPACE_PREFIX):
            names.append(name)
    return sorted(names)


def cleanup_baxbench_namespaces(
    *,
    logger: logging.Logger | None = None,
    exclude: Iterable[str] = (),
) -> list[str]:
    """
    Delete every ``baxbench-*`` namespace except those in ``exclude``.

    Returns names of namespaces we attempted to delete.
    """
    log = logger or logging.getLogger(__name__)
    skip = set(exclude)
    deleted: list[str] = []
    for ns in list_baxbench_namespaces():
        if ns in skip:
            log.info("Skipping namespace cleanup for %s (excluded)", ns)
            continue
        log.info("Cleaning up BaxBench namespace %s", ns)
        delete_iteration_namespace(ns, logger=log)
        deleted.append(ns)
    if deleted:
        log.info("Removed %d BaxBench namespace(s): %s", len(deleted), ", ".join(deleted))
    else:
        log.info("No BaxBench namespaces to clean up")
    return deleted


def cleanup_baxbench_namespaces_before_deploy(*, logger: logging.Logger | None = None) -> None:
    if not cleanup_before_deploy_enabled():
        return
    cleanup_baxbench_namespaces(logger=logger)


def cleanup_baxbench_namespaces_after_bench(*, logger: logging.Logger | None = None) -> None:
    if not cleanup_after_bench_enabled():
        return
    cleanup_baxbench_namespaces(logger=logger)

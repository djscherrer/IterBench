"""Resolve a URL load generators outside the cluster can reach (NodePort)."""

from __future__ import annotations

import json
import logging
import subprocess


def resolve_nodeport_target(
    *,
    namespace: str,
    service: str,
    service_port: int,
    node_host: str,
    logger: logging.Logger | None = None,
) -> str:
    """
    Return ``http://<node_host>:<nodePort>`` for a Service exposed as NodePort.

    ``node_host`` should be a cluster node IP/hostname routable from Locust hosts
    (typically the first profile ``worker_nodes`` entry).
    """
    log = logger or logging.getLogger(__name__)
    proc = subprocess.run(
        [
            "kubectl",
            "get",
            "svc",
            service,
            "-n",
            namespace,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"kubectl get svc {service} -n {namespace} failed: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    data = json.loads(proc.stdout)
    ports = data.get("spec", {}).get("ports") or []
    node_port: int | None = None
    for entry in ports:
        if int(entry.get("port", 0)) == int(service_port):
            np = entry.get("nodePort")
            if np is not None:
                node_port = int(np)
                break
    if node_port is None:
        for entry in ports:
            np = entry.get("nodePort")
            if np is not None:
                node_port = int(np)
                break
    if node_port is None:
        raise RuntimeError(
            f"Service {namespace}/{service} has no nodePort (add type NodePort in manifests)"
        )
    target = f"http://{node_host}:{node_port}"
    log.info("Load target for Locust (NodePort): %s", target)
    return target

"""Resolve a URL load generators outside the cluster can reach (NodePort)."""

from __future__ import annotations

import json
import logging
import subprocess

import remote_exec


def resolve_nodeport_target(
    *,
    namespace: str,
    service: str,
    service_port: int,
    node_host: str,
    logger: logging.Logger | None = None,
) -> str:
    """
    Return ``http://<node_ip>:<nodePort>`` for a Service exposed as NodePort.

    ``node_host`` should be a cluster node short hostname or IP routable from Locust
    hosts (typically the first profile ``worker_nodes`` entry). On Emulab/CloudLab the
    hostname is resolved to the experiment LAN ``10.x`` address from ``/etc/hosts`` so
    Locust does not hit the shared control network.
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
    reach_host = node_host
    try:
        reach_host = remote_exec.resolve_remote_preferred_ipv4(node_host, log)
    except Exception as exc:
        log.warning(
            "Could not resolve experiment IP for NodePort host %s (%s); using hostname",
            node_host,
            exc,
        )
    target = f"http://{reach_host}:{node_port}"
    log.info("Load target for Locust (NodePort): %s (from %s)", target, node_host)
    return target

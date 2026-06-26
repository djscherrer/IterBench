"""
Private Docker registry for lab kubeadm clusters (HTTP on port 5000).

Runs ``registry:2`` on the control-plane host and configures containerd on
every cluster node to pull from it (images cached with imagePullPolicy: IfNotPresent).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import textwrap
from dataclasses import dataclass
from typing import Sequence

from .profiles import resolve_cluster_profile, selected_cluster_profile
from .preflight import (
    _dedupe_hosts,
    _is_local_host,
    _run_shell_on_host,
    apply_cluster_profile_to_env,
)
from .setup import _control_plane_ip


@dataclass(frozen=True)
class RegistryConfig:
    host: str
    port: int = 5000

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.endpoint}"


def _local_primary_ipv4() -> str:
    proc = subprocess.run(
        [
            "bash",
            "-lc",
            "ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i==\"src\") {print $(i+1); exit}}' "
            "|| hostname -I | awk '{print $1}'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    for ip in ips:
        if not ip.startswith("127."):
            return ip
    raise RuntimeError("Could not determine local primary IPv4 for registry host")


def resolve_registry_config(profile_name: str | None = None, *, logger: logging.Logger | None = None) -> RegistryConfig | None:
    """Return registry settings from env or cluster profile."""
    env_ep = (os.environ.get("BAXBENCH_REGISTRY") or "").strip()
    if env_ep:
        if ":" in env_ep:
            host, port_s = env_ep.rsplit(":", 1)
            return RegistryConfig(host=host, port=int(port_s))
        return RegistryConfig(host=env_ep)

    name = (profile_name or "").strip()
    if not name:
        return None
    try:
        profile = resolve_cluster_profile(name)
    except ValueError:
        return None
    if not profile.registry_enabled:
        return None

    host = (profile.registry_host or "").strip()
    if not host or profile.registry_auto_host:
        host = _local_primary_ipv4()
    return RegistryConfig(host=host, port=profile.registry_port)


def _containerd_certs_dir(endpoint: str) -> str:
    return f"/etc/containerd/certs.d/{endpoint}"


def _hosts_toml(registry: RegistryConfig) -> str:
    base = registry.base_url
    return textwrap.dedent(
        f"""\
        server = "{base}"

        [host."{base}"]
          capabilities = ["pull", "resolve", "push"]
          skip_verify = true
        """
    )


def _registry_container_running_local(logger: logging.Logger) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", "baxbench-registry"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    running = (proc.stdout or "").strip() == "true"
    if running:
        logger.info("Registry container baxbench-registry already running")
    return running


def _start_registry_local(registry: RegistryConfig, logger: logging.Logger) -> None:
    if _registry_container_running_local(logger):
        return
    logger.info("Starting local Docker registry on port %s", registry.port)
    proc = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--restart=always",
            "--name",
            "baxbench-registry",
            "-p",
            f"{registry.port}:5000",
            "-e",
            "REGISTRY_STORAGE_DELETE_ENABLED=true",
            "registry:2",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to start registry:2 container: {(proc.stderr or proc.stdout).strip()}"
        )
    logger.info("Registry listening at %s (and http://127.0.0.1:%s)", registry.endpoint, registry.port)


def _configure_containerd_script(registry: RegistryConfig) -> str:
    """
    Ship two things to a worker so kubelet pulls work from our HTTP registry:

    1. ``/etc/containerd/certs.d/<endpoint>/hosts.toml`` — per-registry config
       enabling plain HTTP + ``skip_verify``.
    2. Patch ``/etc/containerd/config.toml`` to set
       ``config_path = "/etc/containerd/certs.d"`` under the CRI registry
       section, so containerd actually *reads* (1). Without this, kubelet
       pulls bypass certs.d and try HTTPS against our HTTP registry.

    Restart containerd after the patch is applied. Idempotent: a second
    invocation only writes hosts.toml; the config.toml stanza is left alone
    if ``config_path = "/etc/containerd/certs.d"`` is already present.
    """
    ep = registry.endpoint
    certs_dir = _containerd_certs_dir(ep)
    return f"""set -euo pipefail
if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi
$SUDO mkdir -p {shlex.quote(certs_dir)}
cat <<'HOSTS_EOF' | $SUDO tee {shlex.quote(certs_dir)}/hosts.toml >/dev/null
{_hosts_toml(registry)}HOSTS_EOF

CFG=/etc/containerd/config.toml
need_restart=false
if [ ! -f "$CFG" ]; then
  $SUDO mkdir -p /etc/containerd
  $SUDO bash -c "containerd config default > $CFG"
  need_restart=true
fi

# Ensure CRI registry section points at /etc/containerd/certs.d.
if ! $SUDO grep -Eq 'config_path *= *"/etc/containerd/certs\\.d"' "$CFG"; then
  if $SUDO grep -q '\\[plugins."io.containerd.grpc.v1.cri".registry\\]' "$CFG"; then
    # Replace existing config_path = "" line, or insert one after the header.
    if $SUDO grep -Eq 'config_path *= *"[^"]*"' "$CFG"; then
      $SUDO sed -i 's|config_path *= *"[^"]*"|config_path = "/etc/containerd/certs.d"|' "$CFG"
    else
      $SUDO sed -i '/\\[plugins."io.containerd.grpc.v1.cri".registry\\]/a \\  config_path = "/etc/containerd/certs.d"' "$CFG"
    fi
  else
    echo '' | $SUDO tee -a "$CFG" >/dev/null
    echo '[plugins."io.containerd.grpc.v1.cri".registry]' | $SUDO tee -a "$CFG" >/dev/null
    echo '  config_path = "/etc/containerd/certs.d"' | $SUDO tee -a "$CFG" >/dev/null
  fi
  need_restart=true
fi

if [ "$need_restart" = "true" ]; then
  $SUDO systemctl restart containerd
else
  # hosts.toml may have changed even when config.toml didn't; force a reload.
  $SUDO systemctl restart containerd
fi
echo 'containerd configured for {ep} (hosts.toml + config.toml certs.d)'
"""


def _configure_docker_insecure_local(registry: RegistryConfig, logger: logging.Logger) -> None:
    """Allow docker push to the local registry:2 instance (HTTP)."""
    daemon = "/etc/docker/daemon.json"
    entries = [f"127.0.0.1:{registry.port}", f"localhost:{registry.port}", registry.endpoint]
    proc = subprocess.run(["sudo", "-n", "cat", daemon], capture_output=True, text=True)
    data: dict = {}
    if proc.returncode == 0 and (proc.stdout or "").strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
    existing = list(data.get("insecure-registries") or [])
    merged = list(dict.fromkeys([*existing, *entries]))
    if merged == existing:
        logger.info("Docker insecure-registries already include %s", registry.endpoint)
        return
    data["insecure-registries"] = merged
    payload = json.dumps(data, indent=2)
    write = subprocess.run(
        ["sudo", "-n", "tee", daemon],
        input=payload,
        capture_output=True,
        text=True,
    )
    if write.returncode != 0:
        logger.warning(
            "Could not update %s (docker push may fail). Add insecure-registries manually: %s",
            daemon,
            entries,
        )
        return
    subprocess.run(["sudo", "-n", "systemctl", "reload", "docker"], check=False)
    logger.info("Updated docker insecure-registries for %s", registry.endpoint)


def _configure_containerd_on_host(host: str, registry: RegistryConfig, logger: logging.Logger) -> None:
    script = _configure_containerd_script(registry)
    logger.info("Configuring containerd on %s for registry %s", host, registry.endpoint)
    _run_shell_on_host(host, script, logger)


def _docker_push_local(image_ref: str, registry: RegistryConfig, logger: logging.Logger) -> None:
    """Tag and push to localhost registry (docker daemon on build host)."""
    # image_ref is full ref like host:5000/baxbench/foo:tag
    local_ref = image_ref.replace(f"{registry.host}:{registry.port}", f"127.0.0.1:{registry.port}", 1)
    tag = subprocess.run(["docker", "tag", image_ref, local_ref], capture_output=True, text=True)
    if tag.returncode != 0:
        raise RuntimeError(f"docker tag failed: {(tag.stderr or tag.stdout).strip()}")
    push = subprocess.run(["docker", "push", local_ref], capture_output=True, text=True)
    if push.returncode != 0:
        raise RuntimeError(f"docker push failed: {(push.stderr or push.stdout).strip()}")
    # ensure canonical name also exists locally for consistency
    subprocess.run(["docker", "tag", local_ref, image_ref], check=False, capture_output=True)
    logger.info("Pushed %s (via %s)", image_ref, local_ref)


def push_image_to_registry(
    image_id: str,
    *,
    repository: str,
    tag: str,
    profile_name: str | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """
    Tag ``image_id`` as ``<registry>/baxbench/<repository>:<tag>`` and push.

    Returns the image reference to put in Kubernetes manifests.
    """
    log = logger or logging.getLogger(__name__)
    registry = resolve_registry_config(profile_name, logger=log)
    if registry is None:
        raise RuntimeError(
            "No registry configured. Enable registry on the cluster profile or set BAXBENCH_REGISTRY."
        )

    reference = f"{registry.endpoint}/baxbench/{repository}:{tag}"
    local_only = f"baxbench-local/{repository}:{tag}"
    tag_proc = subprocess.run(
        ["docker", "tag", image_id, local_only],
        capture_output=True,
        text=True,
    )
    if tag_proc.returncode != 0:
        raise RuntimeError(f"docker tag failed: {(tag_proc.stderr or tag_proc.stdout).strip()}")

    tag2 = subprocess.run(
        ["docker", "tag", local_only, reference],
        capture_output=True,
        text=True,
    )
    if tag2.returncode != 0:
        raise RuntimeError(f"docker tag to registry name failed: {(tag2.stderr or tag.stdout).strip()}")

    _docker_push_local(reference, registry, log)
    return reference


def run_registry_setup(
    *,
    logger: logging.Logger,
    profile_name: str | None,
    control_plane: str,
    node_hosts: Sequence[str],
    registry_host: str | None = None,
    registry_port: int = 5000,
) -> RegistryConfig:
    """
    Start registry on control-plane and configure containerd on all cluster nodes.
    """
    name = (profile_name or "").strip() or None
    if registry_host:
        registry = RegistryConfig(host=registry_host.strip(), port=registry_port)
    else:
        if _is_local_host(control_plane):
            registry = RegistryConfig(host=_local_primary_ipv4(), port=registry_port)
        else:
            registry = RegistryConfig(
                host=_control_plane_ip(control_plane, logger),
                port=registry_port,
            )

    logger.info("=== BaxBench registry setup ===")
    logger.info("Registry endpoint: %s", registry.endpoint)

    if _is_local_host(control_plane):
        _start_registry_local(registry, logger)
        _configure_docker_insecure_local(registry, logger)
    else:
        raise RuntimeError(
            f"Run k8s_setup_cluster.sh on the control-plane host ({control_plane}), not via SSH to it."
        )

    hosts = _dedupe_hosts(node_hosts)
    if control_plane not in hosts:
        hosts = (control_plane, *hosts)

    for i, h in enumerate(hosts, start=1):
        logger.info("[%d/%d] containerd registry config on %s", i, len(hosts), h)
        _configure_containerd_on_host(h, registry, logger)

    # Smoke test from local docker
    test_pull = subprocess.run(
        ["docker", "pull", f"{registry.endpoint}/library/hello-world:latest"],
        capture_output=True,
        text=True,
    )
    if test_pull.returncode != 0:
        logger.warning(
            "Registry smoke pull failed (may be OK if offline): %s",
            (test_pull.stderr or test_pull.stdout).strip()[:200],
        )
    else:
        logger.info("Registry smoke pull OK")

    logger.info(
        "Registry ready. Set BAXBENCH_REGISTRY=%s or use profile with registry_enabled=true",
        registry.endpoint,
    )
    return registry


def run_registry_setup_from_args(args) -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("baxbench.k8s.registry")
    prof = selected_cluster_profile(args=args)
    apply_cluster_profile_to_env(prof.name)

    reg_host = prof.registry_host.strip() or None
    port = prof.registry_port

    run_registry_setup(
        logger=logger,
        profile_name=prof.name,
        control_plane=prof.control_node,
        node_hosts=list(prof.k8s_ssh_hosts),
        registry_host=reg_host,
        registry_port=port,
    )

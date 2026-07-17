"""
Bootstrap a kubeadm cluster: init on control-plane, install CNI, join workers.

Run after k8s-preflight (packages installed). Safe to re-run: skips init/join
when already done.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import remote_exec

from .metrics_server import install_metrics_server
from .profiles import resolve_cluster_profile, selected_cluster_profile
from .preflight import (
    _dedupe_hosts,
    _is_local_host,
    _run_shell_on_host,
    _tail_output,
    apply_cluster_profile_to_env,
)

_FLANNEL_MANIFEST = (
    "https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml"
)


@dataclass(frozen=True)
class SetupClusterResult:
    ok: bool
    message: str
    control_plane: str
    workers: tuple[str, ...]


def _kubeconfig_path(profile_name: str | None) -> Path:
    override = (os.environ.get("KUBECONFIG") or "").strip()
    if override:
        return Path(os.path.expanduser(override.split(os.pathsep)[0]))
    if profile_name:
        profile = resolve_cluster_profile(profile_name)
        if profile.kubeconfig_path:
            return Path(os.path.expanduser(profile.kubeconfig_path))
    raise ValueError(
        "No kubeconfig path: set KUBECONFIG_PATH in scripts/k8s_setup_cluster.sh "
        "or use a cluster profile with kubeconfig_path."
    )


def _host_error(host: str, step: str, out: str) -> str:
    return f"{step} failed on {host}:\n\n{_tail_output(out)}"


def _shell(host: str, script: str, logger: logging.Logger) -> str:
    proc = _run_shell_on_host(host, script, logger)
    out = (proc.stdout or b"").decode(errors="ignore")
    if proc.stderr:
        out += "\n" + (proc.stderr or b"").decode(errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(out)
    return out


def _lab_ipv4(host: str, logger: logging.Logger) -> str:
    """
    Prefer Emulab/CloudLab experiment LAN (``10.x`` from ``/etc/hosts``) over the
    control-network address that the default route uses.
    """
    if _is_local_host(host):
        short = (host or "").strip().split("@")[-1].split(".")[0] or "node0"
        for cmd in (
            f"getent ahostsv4 {shlex.quote(short)} 2>/dev/null | awk '{{print $1; exit}}'",
            "ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1",
        ):
            proc = subprocess.run(
                ["bash", "-lc", cmd], capture_output=True, text=True, check=False
            )
            text = (proc.stdout or "") + (proc.stderr or "")
            for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
                if ip.startswith("10.") and not ip.startswith("127."):
                    return ip
        raise RuntimeError(f"Could not determine experiment LAN IPv4 on local host {host}")
    return remote_exec.resolve_remote_preferred_ipv4(host, logger)


def _control_plane_ip(host: str, logger: logging.Logger) -> str:
    """API advertise / registry host IP (experiment LAN when present)."""
    return _lab_ipv4(host, logger)


def _is_cluster_initialized(control_plane: str, logger: logging.Logger) -> bool:
    script = "test -f /etc/kubernetes/admin.conf && echo yes || echo no"
    return _shell(control_plane, script, logger).strip().endswith("yes")


def _kubeadm_init(control_plane: str, pod_cidr: str, advertise_ip: str, logger: logging.Logger) -> None:
    logger.info(
        "Initializing cluster on %s (apiserver-advertise-address=%s, node-ip=%s, pod-network-cidr=%s)",
        control_plane,
        advertise_ip,
        advertise_ip,
        pod_cidr,
    )
    # Use a small kubeadm config so both the API advertise address and the
    # control-plane kubelet InternalIP use the experiment LAN (not control-net).
    script = f"""set -euo pipefail
if [ -f /etc/kubernetes/admin.conf ]; then
  echo 'SKIP: cluster already initialized (/etc/kubernetes/admin.conf exists)'
  exit 0
fi
if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi
CFG="$(mktemp)"
trap 'rm -f "$CFG"' EXIT
cat >"$CFG" <<EOF
apiVersion: kubeadm.k8s.io/v1beta3
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: "{advertise_ip}"
  bindPort: 6443
nodeRegistration:
  kubeletExtraArgs:
    node-ip: "{advertise_ip}"
---
apiVersion: kubeadm.k8s.io/v1beta3
kind: ClusterConfiguration
networking:
  podSubnet: "{pod_cidr}"
EOF
$SUDO kubeadm init --config="$CFG"
echo 'kubeadm init done'
"""
    out = _shell(control_plane, script, logger)
    logger.info("%s", _tail_output(out, max_lines=15))


def _install_kubeconfig(control_plane: str, dest: Path, logger: logging.Logger) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Writing kubeconfig to %s", dest)
    if _is_local_host(control_plane):
        proc = subprocess.run(
            ["sudo", "-n", "cat", "/etc/kubernetes/admin.conf"],
            capture_output=True,
            check=False,
        )
    else:
        proc = remote_exec.ssh(
            control_plane,
            "sudo -n cat /etc/kubernetes/admin.conf",
            logger,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            "Cannot read /etc/kubernetes/admin.conf (run kubeadm init on control-plane first)"
        )
    dest.write_bytes(proc.stdout or b"")
    dest.chmod(0o600)


def _ensure_flannel_experiment_iface(
    kubeconfig: Path, reach_ip: str, logger: logging.Logger
) -> None:
    """
    Pin flannel VXLAN to the experiment LAN.

    By default flannel picks the default-route NIC (CloudLab control net ``eno1`` /
    ``155.98.*``). Pod↔pod and NodePort→remote-pod traffic then floods the control
    network.

    Emulab experiment NICs are not uniformly named (``enp4s0f1`` vs ``enp6s0f1``),
    so we pass both ``--iface`` values. Do **not** use ``--iface-can-reach=<local
    advertise IP>``: on the control-plane that IP is local and flannel binds ``lo``.
    ``reach_ip`` is kept for logging / future use.
    """
    _ = reach_ip
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    desired = ("--iface=enp4s0f1", "--iface=enp6s0f1")
    get = subprocess.run(
        [
            "kubectl",
            "get",
            "daemonset",
            "-n",
            "kube-flannel",
            "kube-flannel-ds",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].args}",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if get.returncode != 0:
        raise RuntimeError(
            f"Cannot read kube-flannel-ds args:\n{(get.stderr or get.stdout).strip()}"
        )
    current = get.stdout or ""
    if all(flag in current for flag in desired) and "--iface-can-reach=" not in current:
        logger.info("Flannel already pinned to experiment LAN ifaces (%s)", current.strip())
        return
    # Rebuild args: keep non-iface flags, drop iface-can-reach / old iface, append ours.
    try:
        args = json.loads(current) if current.strip().startswith("[") else []
    except json.JSONDecodeError:
        args = []
    if not isinstance(args, list):
        args = []
    cleaned = [
        a
        for a in args
        if not str(a).startswith("--iface=") and not str(a).startswith("--iface-can-reach=")
    ]
    new_args = cleaned + list(desired)
    logger.info("Pinning flannel VXLAN to experiment LAN ifaces: %s", new_args)
    patch = subprocess.run(
        [
            "kubectl",
            "patch",
            "daemonset",
            "-n",
            "kube-flannel",
            "kube-flannel-ds",
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "replace",
                        "path": "/spec/template/spec/containers/0/args",
                        "value": new_args,
                    }
                ]
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if patch.returncode != 0:
        raise RuntimeError(
            f"Failed to patch flannel iface:\n{(patch.stderr or patch.stdout).strip()}"
        )


def _install_cni(
    cni: str,
    kubeconfig: Path,
    logger: logging.Logger,
    *,
    flannel_reach_ip: str | None = None,
) -> None:
    if cni != "flannel":
        raise ValueError(f"Unsupported CNI {cni!r}; only 'flannel' is implemented")
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    check = subprocess.run(
        ["kubectl", "get", "daemonset", "-n", "kube-flannel", "kube-flannel-ds"],
        env=env,
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        logger.info("CNI already installed (kube-flannel-ds present)")
    else:
        logger.info("Installing Flannel CNI (%s)", _FLANNEL_MANIFEST)
        apply = subprocess.run(
            ["kubectl", "apply", "-f", _FLANNEL_MANIFEST],
            env=env,
            capture_output=True,
            text=True,
        )
        if apply.returncode != 0:
            raise RuntimeError(
                f"kubectl apply flannel failed:\n{(apply.stderr or apply.stdout).strip()}"
            )
    if flannel_reach_ip:
        _ensure_flannel_experiment_iface(kubeconfig, flannel_reach_ip, logger)


def _join_command(control_plane: str, logger: logging.Logger) -> str:
    script = """set -euo pipefail
if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi
$SUDO kubeadm token create --print-join-command
"""
    out = _shell(control_plane, script, logger).strip()
    line = ""
    for ln in out.splitlines():
        if "kubeadm join" in ln:
            line = ln.strip()
    if not line:
        raise RuntimeError(f"No join command in kubeadm output:\n{out}")
    return line


def _worker_already_joined(host: str, logger: logging.Logger) -> bool:
    script = "test -f /etc/kubernetes/kubelet.conf && echo yes || echo no"
    try:
        return _shell(host, script, logger).strip().endswith("yes")
    except RuntimeError:
        return False


def _kubeadm_join(worker: str, join_cmd: str, logger: logging.Logger) -> None:
    if _worker_already_joined(worker, logger):
        logger.info("[%s] already joined — skip", worker)
        return
    # join_cmd is like: kubeadm join 10.x.x.x:6443 --token ... --discovery-token-ca-cert-hash ...
    parts = join_cmd.split()
    if parts and parts[0] == "kubeadm":
        parts = parts[1:]
    if not parts or parts[0] != "join" or len(parts) < 2:
        raise RuntimeError(f"Unexpected kubeadm join command: {join_cmd!r}")
    api_endpoint = parts[1]
    token = ""
    ca_hash = ""
    i = 2
    while i < len(parts):
        if parts[i] == "--token" and i + 1 < len(parts):
            token = parts[i + 1]
            i += 2
            continue
        if parts[i].startswith("--token="):
            token = parts[i].split("=", 1)[1]
            i += 1
            continue
        if parts[i] == "--discovery-token-ca-cert-hash" and i + 1 < len(parts):
            ca_hash = parts[i + 1]
            i += 2
            continue
        if parts[i].startswith("--discovery-token-ca-cert-hash="):
            ca_hash = parts[i].split("=", 1)[1]
            i += 1
            continue
        i += 1
    if not token or not ca_hash:
        raise RuntimeError(f"Could not parse token/ca-hash from join command: {join_cmd!r}")

    node_ip = _lab_ipv4(worker, logger)
    logger.info("[%s] joining cluster (node-ip=%s, api=%s)", worker, node_ip, api_endpoint)
    # kubeadm 1.29 has no --node-ip join flag; set it via JoinConfiguration.
    script = f"""set -euo pipefail
if [ -f /etc/kubernetes/kubelet.conf ]; then
  echo 'SKIP: already joined'
  exit 0
fi
if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi
CFG="$(mktemp)"
trap 'rm -f "$CFG"' EXIT
cat >"$CFG" <<EOF
apiVersion: kubeadm.k8s.io/v1beta3
kind: JoinConfiguration
discovery:
  bootstrapToken:
    apiServerEndpoint: "{api_endpoint}"
    token: "{token}"
    caCertHashes:
      - "{ca_hash}"
nodeRegistration:
  kubeletExtraArgs:
    node-ip: "{node_ip}"
EOF
$SUDO kubeadm join --config="$CFG"
echo 'join done'
"""
    out = _shell(worker, script, logger)
    logger.info("[%s] OK\n%s", worker, _tail_output(out, max_lines=6))


def _wait_nodes_ready(kubeconfig: Path, expected: int, logger: logging.Logger, *, timeout_s: int = 600) -> None:
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = subprocess.run(
            ["kubectl", "get", "nodes", "--no-headers"],
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
            ready = sum(
                1
                for ln in lines
                if len(ln.split()) >= 2 and ln.split()[1] == "Ready"
            )
            logger.info("Nodes: %d total, %d Ready (want %d)", len(lines), ready, expected)
            if len(lines) >= expected and ready >= expected:
                logger.info("All nodes Ready:\n%s", (proc.stdout or "").strip())
                return
        time.sleep(10)
    raise RuntimeError(f"Timed out after {timeout_s}s waiting for {expected} Ready nodes")


def run_k8s_setup_cluster(
    *,
    logger: logging.Logger,
    control_plane: str,
    worker_hosts: Sequence[str],
    profile_name: str | None = None,
    pod_network_cidr: str = "10.244.0.0/16",
    cni: str = "flannel",
    skip_cni: bool = False,
    wait_timeout_s: int = 600,
) -> SetupClusterResult:
    cp = control_plane.strip()
    workers = _dedupe_hosts(h for h in worker_hosts if h.strip() and h.strip() != cp)
    if not cp:
        raise ValueError("control_plane host is required")

    apply_cluster_profile_to_env(profile_name)
    kubeconfig = _kubeconfig_path(profile_name)

    logger.info("=== BaxBench k8s setup-cluster ===")
    logger.info("Control-plane: %s", cp)
    logger.info("Workers: %s", ", ".join(workers) if workers else "<none>")
    logger.info("Kubeconfig: %s", kubeconfig)

    advertise_ip = _control_plane_ip(cp, logger)
    logger.info("API advertise address: %s", advertise_ip)

    if not _is_cluster_initialized(cp, logger):
        _kubeadm_init(cp, pod_network_cidr, advertise_ip, logger)
    else:
        logger.info("Control-plane already initialized — skipping kubeadm init")

    _install_kubeconfig(cp, kubeconfig, logger)
    os.environ["KUBECONFIG"] = str(kubeconfig)

    if not skip_cni:
        _install_cni(cni, kubeconfig, logger, flannel_reach_ip=advertise_ip)

    if workers:
        join_cmd = _join_command(cp, logger)
        for i, w in enumerate(workers, start=1):
            logger.info("--- worker %d/%d: %s ---", i, len(workers), w)
            _kubeadm_join(w, join_cmd, logger)

    expected_nodes = 1 + len(workers)
    _wait_nodes_ready(kubeconfig, expected_nodes, logger, timeout_s=wait_timeout_s)

    install_metrics_server(kubeconfig, logger, wait_timeout_s=min(wait_timeout_s, 180))

    logger.info("Cluster setup complete. Set K8S_SKIP_CLUSTER_CHECKS=false and re-run k8s_preflight.")
    return SetupClusterResult(
        ok=True,
        message="ok",
        control_plane=cp,
        workers=workers,
    )


def run_setup_from_args(args: Any) -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("baxbench.k8s.setup_cluster")

    prof = selected_cluster_profile(args=args)
    apply_cluster_profile_to_env(prof.name)

    if not prof.control_node.strip():
        raise ValueError(f"Profile '{prof.name}' has no control_node")
    workers = list(prof.worker_nodes)
    if not workers:
        raise ValueError(f"Profile '{prof.name}' has no worker_nodes")

    pod_cidr = (getattr(args, "k8s_pod_network_cidr", None) or "10.244.0.0/16").strip()
    cni = (getattr(args, "k8s_cni", None) or "flannel").strip()

    run_k8s_setup_cluster(
        logger=logger,
        control_plane=prof.control_node,
        worker_hosts=workers,
        profile_name=prof.name,
        pod_network_cidr=pod_cidr,
        cni=cni,
        skip_cni=bool(getattr(args, "k8s_skip_cni", False)),
        wait_timeout_s=int(getattr(args, "k8s_wait_timeout", None) or 600),
    )

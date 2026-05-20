from __future__ import annotations

import concurrent.futures
import logging
import os
import shlex
import socket
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence

import remote_exec

from .profiles import K8sClusterProfile, resolve_cluster_profile, selected_cluster_profile


@dataclass(frozen=True)
class K8sPreflightResult:
    ok: bool
    message: str


def apply_cluster_profile_to_env(profile_name: str | None) -> None:
    if not profile_name:
        return
    profile = resolve_cluster_profile(profile_name)
    for key, value in profile.to_env().items():
        if value:
            os.environ[key] = value


def _effective_kubeconfig_path() -> str | None:
    raw = (os.environ.get("KUBECONFIG") or "").strip()
    if not raw:
        default = os.path.expanduser("~/.kube/config")
        return default if os.path.isfile(default) else None
    path = os.path.expanduser(raw.split(os.pathsep)[0])
    return path


def _validate_kubeconfig_file(logger: logging.Logger) -> None:
    path = _effective_kubeconfig_path()
    if path is None:
        raise RuntimeError(
            "No kubeconfig found. Set KUBECONFIG_PATH in scripts/k8s_preflight.sh, "
            "copy the control-plane admin.conf to your kubeconfig path (see cluster profile), "
            "or set K8S_SKIP_CLUSTER_CHECKS=true before the cluster exists."
        )
    if not os.path.isfile(path):
        raise RuntimeError(
            f"KUBECONFIG file does not exist: {path}\n"
            "On the control-plane after kubeadm init, copy /etc/kubernetes/admin.conf "
            "to the machine where you run BaxBench, e.g.\n"
            "  mkdir -p /tmp/dscherre/.kube\n"
            "  sudo cp /etc/kubernetes/admin.conf /tmp/dscherre/.kube/config-baxbench-emulab\n"
            "  chmod 600 /tmp/dscherre/.kube/config-baxbench-emulab\n"
            "Or set K8S_SKIP_CLUSTER_CHECKS=true to only run SSH node checks."
        )
    logger.info("kubeconfig: %s", path)


def _kubectl(args: Sequence[str], *, timeout_s: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _require_kubectl(logger: logging.Logger) -> None:
    proc = subprocess.run(["kubectl", "version", "--client"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "kubectl not found or not working. Install kubectl and ensure it is on PATH."
        )
    logger.info("kubectl client: %s", (proc.stdout or proc.stderr or "").strip().splitlines()[0])


def _check_cluster_api(logger: logging.Logger) -> str:
    ctx = _kubectl(["config", "current-context"])
    if ctx.returncode != 0:
        path = _effective_kubeconfig_path() or "<unknown>"
        msg = (ctx.stderr or ctx.stdout).strip()
        raise RuntimeError(
            f"kubectl has no usable context ({msg}).\n"
            f"Kubeconfig: {path}\n"
            "Ensure the file exists and defines current-context (copy admin.conf from "
            "the control-plane). Before the cluster exists, set K8S_SKIP_CLUSTER_CHECKS=true."
        )
    context = (ctx.stdout or "").strip()
    logger.info("kubectl context: %s", context)

    info = _kubectl(["cluster-info"], timeout_s=120)
    if info.returncode != 0:
        raise RuntimeError(f"kubectl cluster-info failed:\n{(info.stderr or info.stdout).strip()}")
    logger.info("cluster-info OK")

    nodes = _kubectl(["get", "nodes", "-o", "wide"], timeout_s=60)
    if nodes.returncode != 0:
        raise RuntimeError(f"kubectl get nodes failed:\n{(nodes.stderr or nodes.stdout).strip()}")
    logger.info("nodes:\n%s", (nodes.stdout or "").strip())

    not_ready = _kubectl(
        ["get", "nodes", "--no-headers", "-o", "custom-columns=NAME:.metadata.name,READY:.status.conditions[-1].status"],
    )
    if not_ready.returncode == 0:
        bad = [
            line
            for line in (not_ready.stdout or "").splitlines()
            if line.strip() and not line.strip().endswith("True")
        ]
        if bad:
            raise RuntimeError("Some nodes are not Ready:\n" + "\n".join(bad))

    return context


def _check_kube_system(logger: logging.Logger) -> None:
    pods = _kubectl(["get", "pods", "-n", "kube-system", "--no-headers"], timeout_s=90)
    if pods.returncode != 0:
        raise RuntimeError(f"cannot list kube-system pods:\n{(pods.stderr or pods.stdout).strip()}")
    lines = [ln for ln in (pods.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("kube-system has no pods — is the CNI installed?")
    bad = [ln for ln in lines if "Running" not in ln and "Completed" not in ln]
    if bad:
        logger.warning("Some kube-system pods not Running/Completed:\n%s", "\n".join(bad[:20]))


def _check_dry_run_apply(logger: logging.Logger) -> None:
    ns = "baxbench-preflight"
    manifest = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {ns}\n"
    apply = subprocess.run(
        ["kubectl", "apply", "--dry-run=server", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if apply.returncode != 0:
        raise RuntimeError(f"kubectl apply --dry-run=server failed:\n{(apply.stderr or apply.stdout).strip()}")
    logger.info("kubectl apply --dry-run=server OK (can reach API with write permissions)")


def ensure_k8s_cluster_ready(
    *,
    logger: logging.Logger | None = None,
    profile_name: str | None = None,
) -> str:
    """
    Fail fast if the Kubernetes cluster is not reachable and healthy.

    Used by k8s-bench before deploy/Locust. Same checks as preflight with
    ``skip_cluster_checks=false`` (kubeconfig, cluster-info, nodes Ready,
    kube-system, dry-run apply).
    """
    log = logger or logging.getLogger("baxbench.k8s.cluster")
    name = (profile_name or os.environ.get("BAXBENCH_K8S_CLUSTER", "") or "").strip() or None
    if name:
        apply_cluster_profile_to_env(name)
        log.info("Using K8s cluster profile: %s", name)

    log.info("Checking Kubernetes cluster is up before k8s-bench...")
    _require_kubectl(log)
    _validate_kubeconfig_file(log)
    context = _check_cluster_api(log)
    _check_kube_system(log)
    _check_dry_run_apply(log)
    log.info("Kubernetes cluster OK (context=%s)", context)
    return context


def _k8s_apt_channel() -> str:
    return (os.environ.get("BAXBENCH_K8S_APT_CHANNEL") or "v1.29").strip()


def _dedupe_hosts(hosts: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in hosts:
        h = raw.strip()
        if not h:
            continue
        key = h.lower().split(".", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return tuple(out)


def _local_host_keys() -> set[str]:
    keys: set[str] = {"localhost", "127.0.0.1"}
    for name in (socket.gethostname(), os.environ.get("HOSTNAME", "")):
        if name:
            keys.add(name.lower().split(".", 1)[0])
    return keys


def _is_local_host(host: str) -> bool:
    return host.strip().lower().split(".", 1)[0] in _local_host_keys()


def _tail_output(text: str, *, max_lines: int = 30) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(["... (truncated) ...", *lines[-max_lines:]])


def _format_host_error(host: str, out: str, *, install: bool) -> str:
    hint = ""
    if "Could not get lock" in out or "Unable to lock directory" in out:
        hint = (
            "\nHint: apt was busy on this host (parallel apt or unattended-upgrades). "
            "Wait a minute and re-run preflight; installs run one node at a time now."
        )
    if "sudo: a password is required" in out or "sudo -n" in out:
        hint += "\nHint: passwordless sudo is required (sudo -n) on cluster nodes."
    action = "install" if install else "check"
    return f"Preflight {action} failed on {host}:{hint}\n\n{_tail_output(out)}"


def _ssh_prereq_check_cmd() -> str:
    # Before kubeadm init/join, kubelet may not be Running yet — only require binaries + containerd.
    return (
        "set -euo pipefail; "
        "echo '--- swap ---'; swapon --show || true; "
        "test -z \"$(swapon --show | tail -n +2)\" || { echo 'FAIL: swap enabled'; exit 1; }; "
        "echo '--- containerd ---'; command -v containerd; systemctl is-active containerd; "
        "echo '--- kubernetes bins ---'; command -v kubeadm; command -v kubectl; command -v kubelet; "
        "systemctl is-enabled kubelet >/dev/null 2>&1 || echo 'WARN: kubelet not enabled yet'; "
        "echo 'OK (ready for kubeadm init/join)'"
    )


def _ssh_install_prerequisites_cmd() -> str:
    """Ubuntu/Debian-oriented node prep (does NOT run kubeadm init/join)."""
    channel = _k8s_apt_channel()
    return rf"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi

wait_apt() {{
  local n=0
  while [ "$n" -lt 60 ]; do
    if ! $SUDO fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
       && ! $SUDO fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then
      return 0
    fi
    n=$((n + 1))
    echo "  waiting for apt lock (${{n}}/60)..."
    sleep 5
  done
  echo "ERROR: apt lock still held after 5 minutes" >&2
  return 1
}}

apt_run() {{
  wait_apt
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 "$@"
}}

echo "[1/5] disable swap"
$SUDO swapoff -a 2>/dev/null || true
$SUDO sed -i '/ swap / s/^/#/' /etc/fstab

echo "[2/5] kernel modules + sysctl"
$SUDO modprobe overlay
$SUDO modprobe br_netfilter
$SUDO tee /etc/sysctl.d/k8s.conf >/dev/null <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
$SUDO sysctl --system 2>/dev/null || true

echo "[3/5] containerd"
if ! command -v containerd >/dev/null 2>&1; then
  apt_run update -qq
  apt_run install -y -qq containerd apt-transport-https ca-certificates curl gpg
fi
$SUDO mkdir -p /etc/containerd
containerd config default | $SUDO tee /etc/containerd/config.toml >/dev/null
$SUDO sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
$SUDO systemctl enable containerd
$SUDO systemctl restart containerd

echo "[4/5] kubeadm kubelet kubectl ({channel})"
if ! command -v kubeadm >/dev/null 2>&1; then
  $SUDO install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://pkgs.k8s.io/core:/stable:/{channel}/deb/Release.key | $SUDO gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
  echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/{channel}/deb/ /" | $SUDO tee /etc/apt/sources.list.d/kubernetes.list
  apt_run update -qq
  apt_run install -y -qq kubelet kubeadm kubectl
  $SUDO apt-mark hold kubelet kubeadm kubectl
fi
$SUDO systemctl enable kubelet || true

echo "[5/5] verify"
command -v kubeadm && kubeadm version -o short
command -v kubectl && kubectl version --client=true -o short 2>/dev/null || true
systemctl is-active containerd
echo 'OK: run sudo kubeadm init on control-plane, then kubeadm join on each worker'
"""


def _prepare_load_host(host: str, logger: logging.Logger, *, install: bool) -> None:
    """Locust hosts: python3 + venv + pip (same as distributed bench load generators)."""
    logger.info("Preflight: %s load host %s", "preparing" if install else "checking", host)
    prev = os.environ.get("BAXBENCH_AUTO_INSTALL_REMOTE_DEPS")
    if install:
        os.environ["BAXBENCH_AUTO_INSTALL_REMOTE_DEPS"] = "1"
    try:
        remote_exec.ensure_remote_python_tooling(host, logger)
    finally:
        if prev is None:
            os.environ.pop("BAXBENCH_AUTO_INSTALL_REMOTE_DEPS", None)
        else:
            os.environ["BAXBENCH_AUTO_INSTALL_REMOTE_DEPS"] = prev


def _run_shell_on_host(host: str, cmd: str, logger: logging.Logger) -> subprocess.CompletedProcess[bytes]:
    """Run a bash script on host (local shell or SSH). Suppresses full SSH command spam."""
    prev = os.environ.get("BAXBENCH_LOG_COMMANDS")
    os.environ["BAXBENCH_LOG_COMMANDS"] = "0"
    try:
        if _is_local_host(host):
            logger.info("  → running locally on %s (no SSH)", host)
            return subprocess.run(
                ["bash", "-lc", cmd],
                capture_output=True,
                check=False,
            )
        logger.info("  → ssh %s", host)
        result = remote_exec.ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)
        if result.returncode == 0:
            lines = (result.stdout or b"").decode(errors="replace").strip().splitlines()
            if lines:
                logger.info("  ✓ %s: %s", host, lines[-1][:200])
        else:
            tail = (result.stdout or b"").decode(errors="replace").strip().splitlines()
            msg = tail[-1] if tail else f"exit {result.returncode}"
            logger.warning("  ✗ %s: %s", host, msg[:300])
        return result
    finally:
        if prev is None:
            os.environ.pop("BAXBENCH_LOG_COMMANDS", None)
        else:
            os.environ["BAXBENCH_LOG_COMMANDS"] = prev


def _check_ssh_node(
    host: str,
    logger: logging.Logger,
    *,
    install: bool,
    index: int,
    total: int,
) -> None:
    label = "install" if install else "check"
    where = "local" if _is_local_host(host) else "ssh"
    logger.info("[%d/%d] K8s node %s — %s (%s)", index, total, host, label, where)
    cmd = _ssh_install_prerequisites_cmd() if install else _ssh_prereq_check_cmd()
    proc = _run_shell_on_host(host, cmd, logger)
    out = (proc.stdout or b"").decode(errors="ignore")
    if proc.stderr:
        out += "\n" + (proc.stderr or b"").decode(errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(_format_host_error(host, out, install=install))
    last = _tail_output(out, max_lines=8)
    logger.info("[%d/%d] %s — OK\n%s", index, total, host, last)


def run_k8s_preflight(
    *,
    logger: logging.Logger,
    profile: K8sClusterProfile,
    install_prerequisites: bool = False,
    skip_cluster_checks: bool = False,
) -> K8sPreflightResult:
    apply_cluster_profile_to_env(profile.name)
    logger.info("Using K8s cluster profile: %s", profile.name)
    if profile.has_k8s_topology():
        logger.info(
            "Profile topology: control=%s workers=[%s]",
            profile.control_node,
            ", ".join(profile.worker_nodes),
        )
    if profile.has_load_topology():
        logger.info(
            "Profile Locust: master=%s workers=[%s]",
            profile.load_master,
            ", ".join(profile.load_workers) or "(none)",
        )

    _require_kubectl(logger)

    if not skip_cluster_checks:
        _validate_kubeconfig_file(logger)
        _check_cluster_api(logger)
        _check_kube_system(logger)
        _check_dry_run_apply(logger)

    hosts = profile.k8s_ssh_hosts

    if hosts:
        total = len(hosts)
        if install_prerequisites:
            logger.info(
                "Installing K8s prerequisites on %d node(s), one at a time (avoids apt locks)",
                total,
            )
            for i, h in enumerate(hosts, start=1):
                _check_ssh_node(h, logger, install=True, index=i, total=total)
        else:
            logger.info("Checking K8s prerequisites on %d node(s) in parallel", total)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, total)) as ex:
                futs = [
                    ex.submit(
                        _check_ssh_node,
                        h,
                        logger,
                        install=False,
                        index=i,
                        total=total,
                    )
                    for i, h in enumerate(hosts, start=1)
                ]
                for fut in futs:
                    fut.result()
    elif install_prerequisites:
        raise ValueError(
            f"install_prerequisites requires control_node and worker_nodes in profile '{profile.name}'"
        )

    load = profile.locust_hosts
    if load:
        logger.info("Locust host(s): %s", ", ".join(load))
        for i, h in enumerate(load, start=1):
            logger.info("[%d/%d] Locust host %s", i, len(load), h)
            _prepare_load_host(h, logger, install=install_prerequisites)

    logger.info("K8s preflight OK.")
    return K8sPreflightResult(ok=True, message="ok")


def run_preflight_from_args(args: Any) -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("baxbench.k8s.preflight")
    prof = selected_cluster_profile(args=args)
    run_k8s_preflight(
        logger=logger,
        profile=prof,
        install_prerequisites=bool(getattr(args, "k8s_install_prerequisites", False)),
        skip_cluster_checks=bool(getattr(args, "k8s_skip_cluster_checks", False)),
    )

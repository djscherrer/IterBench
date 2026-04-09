"""
Remote execution primitives (SSH/SCP/subprocess helpers) used by distributed benchmarking.

This module contains *mechanics* (how we execute remote commands, transfer files, collect logs,
sample host metrics), but not the higher-level benchmark orchestration.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import shlex
import socket
import subprocess
import tarfile
import tempfile
import time
from typing import Tuple

import docker
from fabric import Connection

from env.base import Env
from bench_models import RemoteConfig


_REMOTE_LOAD_PACKAGES = ("locust", "faker", "zope.event==5")
_REMOTE_ENV_MARKER = hashlib.sha256("|".join(_REMOTE_LOAD_PACKAGES).encode("utf-8")).hexdigest()[:12]

_SSH_MULTIPLEX = os.environ.get("BAXBENCH_SSH_MULTIPLEX", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_LOG_COMMANDS = os.environ.get("BAXBENCH_LOG_COMMANDS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_COLLECT_DOCKER_LOGS = os.environ.get("BAXBENCH_COLLECT_DOCKER_LOGS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def ssh_control_path(host: str) -> str:
    """
    Path for SSH ControlMaster socket (kept short to avoid UNIX path limits).
    """
    base = os.environ.get("BAXBENCH_SSH_CONTROL_DIR", os.path.join(tempfile.gettempdir(), "baxbench-ssh"))
    try:
        pathlib.Path(base).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    hid = hashlib.sha256(host.encode("utf-8")).hexdigest()[:10]
    return os.path.join(base, f"cm-{hid}")


def ssh_base_cmd(host: str) -> list[str]:
    cmd = ["ssh"]
    if _SSH_MULTIPLEX:
        cp = ssh_control_path(host)
        cmd += [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=60s",
            "-o",
            f"ControlPath={cp}",
        ]
    cmd.append(host)
    return cmd


def scp_base_cmd(host: str) -> list[str]:
    cmd = ["scp"]
    if _SSH_MULTIPLEX:
        cp = ssh_control_path(host)
        cmd += ["-o", f"ControlPath={cp}"]
    return cmd


def run_subprocess(
    cmd: list[str],
    logger: logging.Logger,
    cwd: pathlib.Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    if _LOG_COMMANDS:
        logger.info("Running command: %s", " ".join(shlex.quote(x) for x in cmd))
    else:
        logger.debug("Running command: %s", " ".join(shlex.quote(x) for x in cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
    if _LOG_COMMANDS:
        logger.info("Command finished with code %s", result)
    else:
        logger.debug("Command finished with code %s", result)
    if result.stdout:
        logger.debug("Command output:\n%s", result.stdout.decode(errors="ignore"))
    return result


def ssh(host: str, command: str, logger: logging.Logger, timeout: int | None = None) -> subprocess.CompletedProcess:
    ssh_cmd = ssh_base_cmd(host) + [command]
    return run_subprocess(ssh_cmd, logger, timeout=timeout)


def ssh_warmup(host: str, logger: logging.Logger) -> None:
    """
    Best-effort: create an SSH ControlMaster connection early.
    """
    if not _SSH_MULTIPLEX:
        return
    cp = ssh_control_path(host)
    cmd = [
        "ssh",
        "-o",
        "ControlMaster=yes",
        "-o",
        "ControlPersist=60s",
        "-o",
        f"ControlPath={cp}",
        "-N",
        "-f",
        host,
    ]
    try:
        run_subprocess(cmd, logger)
    except Exception:
        pass


def ensure_rootless_docker(host: str, logger: logging.Logger) -> None:
    cmd = (
        "set -euo pipefail; "
        "if command -v loginctl >/dev/null 2>&1; then "
        "  loginctl enable-linger \"$USER\" >/dev/null 2>&1 || true; "
        "fi; "
        "if command -v systemctl >/dev/null 2>&1; then "
        "  systemctl --user is-active docker >/dev/null 2>&1 || systemctl --user start docker >/dev/null 2>&1 || true; "
        "fi; "
        "docker info >/dev/null 2>&1 || true"
    )
    ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)


def scp_to_remote(local_path: pathlib.Path, host: str, remote_path: str, logger: logging.Logger) -> None:
    scp_cmd = scp_base_cmd(host) + [str(local_path), f"{host}:{remote_path}"]
    run_subprocess(scp_cmd, logger)


def scp_from_remote(host: str, remote_path: str, local_path: pathlib.Path, logger: logging.Logger) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    scp_cmd = scp_base_cmd(host) + [f"{host}:{remote_path}", str(local_path)]
    run_subprocess(scp_cmd, logger)


def collect_docker_logs_bundle(
    host: str,
    bundle_name: str,
    container_names: list[str],
    remote_base_dir: str,
    local_out_dir: pathlib.Path,
    logger: logging.Logger,
) -> None:
    if not container_names:
        return
    local_out_dir.mkdir(parents=True, exist_ok=True)
    local_bundle_dir = local_out_dir / bundle_name
    local_bundle_dir.mkdir(parents=True, exist_ok=True)

    remote_dir = f"{remote_base_dir.rstrip('/')}/{bundle_name}"
    remote_tgz = f"{remote_dir}.tgz"

    logs_cmd_parts = [
        "set -euo pipefail;",
        f"rm -rf {shlex.quote(remote_dir)} {shlex.quote(remote_tgz)} || true;",
        f"mkdir -p {shlex.quote(remote_dir)};",
    ]
    for cname in container_names:
        logs_cmd_parts.append(
            f"(docker logs --timestamps {shlex.quote(cname)} > {shlex.quote(remote_dir)}/{shlex.quote(cname)}.log 2>&1 || true);"
        )
        logs_cmd_parts.append(
            f"(docker inspect {shlex.quote(cname)} > {shlex.quote(remote_dir)}/{shlex.quote(cname)}.inspect.json 2>&1 || true);"
        )
    logs_cmd_parts.append(
        f"tar -czf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_base_dir.rstrip('/'))} {shlex.quote(bundle_name)};"
    )

    ssh(host, f"bash -lc {shlex.quote(' '.join(logs_cmd_parts))}", logger)
    local_tgz = local_bundle_dir / f"{bundle_name}.tgz"
    scp_from_remote(host, remote_tgz, local_tgz, logger)

    try:
        with tarfile.open(local_tgz, "r:gz") as tf:
            tf.extractall(path=local_bundle_dir)
    except Exception as exc:
        logger.warning("Failed to extract docker logs bundle %s from %s: %s", bundle_name, host, exc)

    ssh(host, f"bash -lc {shlex.quote(f'rm -rf {remote_dir} {remote_tgz} || true')}", logger)


def ensure_remote_python_env(load_host: str, remote_env_dir: str, logger: logging.Logger) -> str:
    env_path = pathlib.PurePosixPath(remote_env_dir)
    parent = str(env_path.parent)
    marker = str(env_path / f".setup-{_REMOTE_ENV_MARKER}")
    venv_python = str(env_path / "bin" / "python")
    locust_bin = str(env_path / "bin" / "locust")
    requirements = " ".join(shlex.quote(pkg) for pkg in _REMOTE_LOAD_PACKAGES)

    mkdir_parent = ""
    if parent and parent != ".":
        mkdir_parent = f"mkdir -p {shlex.quote(parent)}; "

    setup_cmd = (
        "set -euo pipefail; "
        f"{mkdir_parent}"
        f"if [ ! -d {shlex.quote(str(env_path))} ]; then "
        f"python3 -m venv {shlex.quote(str(env_path))}; "
        "fi; "
        f"if [ ! -f {shlex.quote(marker)} ]; then "
        f"{shlex.quote(venv_python)} -m pip install --upgrade pip; "
        f"{shlex.quote(venv_python)} -m pip install {requirements}; "
        f"touch {shlex.quote(marker)}; "
        "fi"
    )

    ssh(load_host, f"bash -lc {shlex.quote(setup_cmd)}", logger)
    return locust_bin


def save_image_tar(image_id: str, out_dir: pathlib.Path, logger: logging.Logger) -> pathlib.Path:
    tar_path = out_dir / f"{image_id[7:][:12]}.tar"
    if tar_path.exists():
        logger.info("Reusing existing docker image tar at %s", tar_path)
        return tar_path
    logger.info("Saving docker image %s to %s", image_id, tar_path)
    client = docker.from_env()
    image = client.images.get(image_id)
    with open(tar_path, "wb") as f:
        for chunk in image.save(named=True):
            f.write(chunk)
    return tar_path


def wait_for_remote_http(
    host: str,
    port: int,
    config: RemoteConfig,
    env: Env,
    logger: logging.Logger,
    probe_host: str | None = None,
) -> None:
    wait_budget = config.max_startup_wait or env.wait_to_start_time
    start = time.time()
    last_exc: Exception | None = None
    while time.time() - start < wait_budget:
        try:
            probe_cmd = "set -euo pipefail; " f"curl -sS -o /dev/null http://{host}:{port}/ --max-time 5"
            out = ssh(probe_host or config.load_host, f'bash -lc "{probe_cmd}"', logger)
            out.check_returncode()
            logger.info("Remote server %s:%d is ready", host, port)
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            logger.info("Remote server not ready yet: %s", exc)
        time.sleep(config.poll_interval)
    raise TimeoutError(f"Remote server {host}:{port} did not respond within {wait_budget} seconds") from last_exc


def resolve_ipv4(hostname: str) -> str:
    try:
        info = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
        return info[0][4][0]
    except socket.gaierror as exc:
        raise ValueError(
            f"Unable to resolve '{hostname}' to an IPv4 address on the orchestrator. "
            "Pass a DNS-resolvable hostname or an explicit IP."
        ) from exc


def resolve_remote_primary_ipv4(host: str, logger: logging.Logger) -> str:
    cmd = (
        "set -euo pipefail; "
        "ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i==\"src\") {print $(i+1); exit}}' "
        "|| hostname -I | awk '{print $1}'"
    )
    out = ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)
    out.check_returncode()
    text = (out.stdout or b"").decode(errors="ignore")
    ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    ip = ips[-1].strip() if ips else ""
    if not ip or ip.startswith("127."):
        raise ValueError(
            f"Unable to determine a non-loopback IPv4 for remote host {host!r}; got output {text!r}"
        )
    return ip


def start_remote_ssh_tunnel(
    host: str,
    tunnel_name: str,
    local_port: int,
    target_host: str,
    target_port: int,
    ssh_dest: str,
    tunnel_dir: str,
    logger: logging.Logger,
    bind_host: str = "127.0.0.1",
) -> str:
    pidfile = f"{tunnel_dir}/{tunnel_name}.pid"
    cmd = (
        "set -euo pipefail; "
        f"mkdir -p {shlex.quote(tunnel_dir)}; "
        f"if [ -f {shlex.quote(pidfile)} ]; then "
        f"  kill $(cat {shlex.quote(pidfile)}) >/dev/null 2>&1 || true; "
        f"  rm -f {shlex.quote(pidfile)}; "
        "fi; "
        "nohup ssh "
        "-o ExitOnForwardFailure=yes "
        "-o StrictHostKeyChecking=accept-new "
        "-o ServerAliveInterval=30 "
        "-o ServerAliveCountMax=3 "
        f"-N -L {bind_host}:{local_port}:{target_host}:{target_port} "
        f"{shlex.quote(ssh_dest)} "
        ">/dev/null 2>&1 & "
        f"echo $! > {shlex.quote(pidfile)}"
    )
    ssh(host, f"bash -lc {shlex.quote(cmd)}", logger).check_returncode()
    return pidfile


def stop_remote_ssh_tunnel(host: str, pidfile: str, logger: logging.Logger) -> None:
    cmd = (
        "set -euo pipefail; "
        f"if [ -f {shlex.quote(pidfile)} ]; then "
        f"  kill $(cat {shlex.quote(pidfile)}) >/dev/null 2>&1 || true; "
        f"  rm -f {shlex.quote(pidfile)}; "
        "fi"
    )
    ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)


def get_cpu_usage(connection: Connection) -> Tuple[int, int]:
    cmd = "cat /proc/stat | grep '^cpu '"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1
    parts = out.stdout.split()
    nums = list(map(int, parts[1:]))
    user, nice, system, idle, iowait, irq, softirq, steal, *_ = nums + [0] * (9 - len(nums))
    idle_all = idle + iowait
    non_idle = user + nice + system + irq + softirq + steal
    total = idle_all + non_idle
    return total, idle_all


def get_memory_usage(connection: Connection) -> Tuple[float, float, float]:
    cmd = "cat /proc/meminfo"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1, -1
    meminfo = {}
    for line in out.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().split()[0]
        try:
            meminfo[key] = int(value)
        except ValueError:
            pass
    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", 0)
    used = total - available
    used_pct = (used / total * 100.0) if total > 0 else 0.0
    total_mb = total / 1024.0
    used_mb = used / 1024.0
    return used_mb, total_mb, used_pct


def get_disk_usage(connection: Connection, disk: str = "sda") -> Tuple[int, int]:
    cmd = f"cat /proc/diskstats | awk '$3==\"{disk}\" {{print $6, $10}}'"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1
    parts = out.stdout.split()
    if len(parts) < 2:
        return -1, -1
    return int(parts[0]), int(parts[1])


def get_network_usage(connection: Connection) -> Tuple[int, int]:
    cmd = "cat /proc/net/dev"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1
    lines = out.stdout.strip().splitlines()
    bytes_rx = 0
    bytes_tx = 0
    for line in lines[2:]:
        _iface, data = line.split(":", 1)
        stats = data.split()
        bytes_rx += int(stats[0])
        bytes_tx += int(stats[8])
    return bytes_rx, bytes_tx


def capture_host_performance(
    sample_dir: pathlib.Path,
    host: str,
    logger: logging.Logger,
    stop_event,
    interval: int = 10,
) -> None:
    filename = sample_dir / "server_performance.csv"
    with open(filename, "w") as f:
        f.write(
            "timestamp,cpu_usage,mem_used_mbytes,mem_free_mbytes,disk_read_bps,disk_write_bps,network_rx_bytes,network_tx_bytes\n"
        )
    connection = Connection(host)
    last_cpu_stats = None
    last_disk_stats = None
    last_net_stats = None
    while not stop_event.is_set():
        loop_start = time.time()
        cpu_stats = get_cpu_usage(connection)
        disk_stats = get_disk_usage(connection)
        mem_stats = get_memory_usage(connection)
        net_stats = get_network_usage(connection)

        cpu_usage = 0
        if last_cpu_stats is not None:
            cpu_usage = 1 - ((cpu_stats[1] - last_cpu_stats[1]) / (cpu_stats[0] - last_cpu_stats[0]))

        disk_reads = 0
        disk_writes = 0
        if last_disk_stats is not None:
            disk_reads = disk_stats[0] - last_disk_stats[0]
            disk_writes = disk_stats[1] - last_disk_stats[1]

        net_rx = 0
        net_tx = 0
        if last_net_stats is not None:
            net_rx = net_stats[0] - last_net_stats[0]
            net_tx = net_stats[1] - last_net_stats[1]

        with open(filename, "a") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts},{cpu_usage},{mem_stats[0]},{mem_stats[1]},{disk_reads},{disk_writes},{net_rx},{net_tx}\n")

        last_cpu_stats = cpu_stats
        last_disk_stats = disk_stats
        last_net_stats = net_stats

        time_to_sleep = interval - (time.time() - loop_start)
        if time_to_sleep > 0:
            time.sleep(time_to_sleep)
    connection.close()


__all__ = [
    "RemoteConfig",
    "_COLLECT_DOCKER_LOGS",
    "collect_docker_logs_bundle",
    "ensure_remote_python_env",
    "ensure_rootless_docker",
    "resolve_ipv4",
    "resolve_remote_primary_ipv4",
    "save_image_tar",
    "scp_from_remote",
    "scp_to_remote",
    "ssh",
    "ssh_warmup",
    "start_remote_ssh_tunnel",
    "stop_remote_ssh_tunnel",
    "wait_for_remote_http",
    "capture_host_performance",
]


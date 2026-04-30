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

_CAPTURE_PERCPU = os.environ.get("BAXBENCH_CAPTURE_PERCPU", "0").strip().lower() in (
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
    cmd = ["ssh", "-o", "BatchMode=yes"]
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
    cmd = ["scp", "-o", "BatchMode=yes"]
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
        # If Docker isn't reachable, fail early so later steps don't produce confusing
        # "No such container" / readiness timeouts.
        "docker info >/dev/null 2>&1"
    )
    out = ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)
    try:
        out.check_returncode()
    except Exception as exc:
        msg = (out.stdout or b"").decode(errors="ignore").strip()
        if not msg:
            msg = f"exit {out.returncode}"
        raise RuntimeError(
            f"Docker is not available on host {host}. "
            f"Ensure the Docker daemon is running and that your user can access it.\n{msg}"
        ) from exc


def scp_to_remote(local_path: pathlib.Path, host: str, remote_path: str, logger: logging.Logger) -> None:
    scp_cmd = scp_base_cmd(host) + [str(local_path), f"{host}:{remote_path}"]
    run_subprocess(scp_cmd, logger).check_returncode()


def scp_from_remote(host: str, remote_path: str, local_path: pathlib.Path, logger: logging.Logger) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    scp_cmd = scp_base_cmd(host) + [f"{host}:{remote_path}", str(local_path)]
    run_subprocess(scp_cmd, logger).check_returncode()


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
            out = ssh(probe_host or config.load_host_master, f'bash -lc "{probe_cmd}"', logger)
            out.check_returncode()
            logger.info("Remote server %s:%d is ready", host, port)
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            logger.info("Remote server not ready yet: %s", exc)
        time.sleep(config.poll_interval)
    raise TimeoutError(f"Remote server {host}:{port} did not respond within {wait_budget} seconds") from last_exc


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


def _is_preferred_ipv4(ip: str, preferred_prefixes: tuple[str, ...]) -> bool:
    return any(ip.startswith(pfx) for pfx in preferred_prefixes)


def resolve_remote_preferred_ipv4(
    host: str,
    logger: logging.Logger,
    *,
    preferred_prefixes: tuple[str, ...] = ("10.233.",),
) -> str:
    """
    Resolve an IPv4 address *on the remote host*.

    Policy:
    - Query all global IPv4 addresses on the remote host.
    - Prefer the first address matching any prefix in preferred_prefixes (default: 10.233.*).
    - Otherwise fall back to the first non-loopback global IPv4.
    - Otherwise fall back to the primary route source IP.
    """
    cmd = (
        "set -euo pipefail; "
        # List all non-loopback global IPv4 addresses (no CIDR), one per line.
        "ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 || true"
    )
    out = ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)
    out.check_returncode()
    text = (out.stdout or b"").decode(errors="ignore")
    ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    ips = [ip for ip in ips if ip and not ip.startswith("127.")]
    preferred = [ip for ip in ips if _is_preferred_ipv4(ip, preferred_prefixes)]
    if preferred:
        return preferred[0]
    if ips:
        return ips[0]
    return resolve_remote_primary_ipv4(host, logger)


def get_cpu_times(connection: Connection) -> tuple[int, int, int, int, int, int, int, int]:
    cmd = "cat /proc/stat | grep '^cpu '"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return (-1, -1, -1, -1, -1, -1, -1, -1)
    parts = out.stdout.split()
    nums = list(map(int, parts[1:]))
    user, nice, system, idle, iowait, irq, softirq, steal, *_ = nums + [0] * (9 - len(nums))
    return (user, nice, system, idle, iowait, irq, softirq, steal)


def get_cpu_times_percpu(connection: Connection) -> dict[str, tuple[int, int, int, int, int, int, int, int]]:
    """
    Return per-CPU times from /proc/stat for cpu0, cpu1, ...
    """
    out = connection.run("cat /proc/stat | grep '^cpu[0-9]'", hide=True, warn=True)
    if not getattr(out, "ok", False):
        return {}
    rows: dict[str, tuple[int, int, int, int, int, int, int, int]] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        cpu = parts[0].strip()
        try:
            nums = list(map(int, parts[1:]))
        except Exception:
            continue
        user, nice, system, idle, iowait, irq, softirq, steal, *_ = nums + [0] * (9 - len(nums))
        rows[cpu] = (user, nice, system, idle, iowait, irq, softirq, steal)
    return rows


def get_loadavg(connection: Connection) -> tuple[float, float, float]:
    out = connection.run("cat /proc/loadavg", hide=True)
    if not out.ok:
        return (-1.0, -1.0, -1.0)
    parts = out.stdout.strip().split()
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except Exception:
        return (-1.0, -1.0, -1.0)


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


def get_swap_usage(connection: Connection) -> Tuple[float, float, float]:
    out = connection.run("cat /proc/meminfo", hide=True)
    if not out.ok:
        return (-1.0, -1.0, -1.0)
    meminfo: dict[str, int] = {}
    for line in out.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().split()[0]
        try:
            meminfo[key] = int(value)
        except ValueError:
            pass
    total = meminfo.get("SwapTotal", 0)
    free = meminfo.get("SwapFree", 0)
    used = total - free
    used_pct = (used / total * 100.0) if total > 0 else 0.0
    total_mb = total / 1024.0
    used_mb = used / 1024.0
    return (used_mb, total_mb, used_pct)


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


def get_docker_stats_cpu_pct(connection: Connection, container: str) -> float | None:
    """
    Return Docker's CPUPerc for a single container (same scale as `docker stats`: 100% ≈ one core).

    This is cgroup-aware and reflects `--cpus` / CPU quota, unlike machine-wide /proc/stat ratios.
    """
    name = (container or "").strip()
    if not name:
        return None
    quoted = shlex.quote(name)
    cmd = f"docker stats {quoted} --no-stream --format '{{{{.CPUPerc}}}}' 2>/dev/null"
    result = connection.run(cmd, hide=True, warn=True)
    if not getattr(result, "ok", False):
        return None
    txt = (result.stdout or "").strip().replace("%", "")
    if not txt or txt.upper() == "N/A":
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def capture_host_performance(
    sample_dir: pathlib.Path,
    host: str,
    logger: logging.Logger,
    stop_event,
    interval: int = 10,
    out_csv: pathlib.Path | None = None,
    docker_container: str | None = None,
) -> None:
    # cpu_usage_ratio is from the first line of /proc/stat (cpu): aggregate across
    # *all* logical CPUs on the host. It is not Docker cgroup CPU and does not
    # reflect --cpus limits on a single container. Compare container limits with
    # `docker stats`, not this series.
    filename = out_csv or (sample_dir / "server_performance.csv")
    percpu_filename: pathlib.Path | None = None
    percpu_cpus: list[str] = []
    if _CAPTURE_PERCPU:
        percpu_filename = filename.with_name(filename.stem + "_percpu.csv")
    with open(filename, "w") as f:
        f.write(
            "ts_epoch_s,ts,cpu_usage_ratio,mem_used_mbytes,mem_total_mbytes,mem_used_pct,"
            "swap_used_mbytes,swap_total_mbytes,swap_used_pct,"
            "loadavg_1,loadavg_5,loadavg_15,"
            "cpu_user_ratio,cpu_system_ratio,cpu_iowait_ratio,cpu_steal_ratio,"
            "disk_read_sectors_delta,disk_write_sectors_delta,network_rx_bytes_delta,network_tx_bytes_delta,"
            "container_cpu_pct\n"
        )
    connection = Connection(host)
    last_cpu_stats = None
    last_cpu_stats_percpu: dict[str, tuple[int, int, int, int, int, int, int, int]] | None = None
    last_disk_stats = None
    last_net_stats = None
    while not stop_event.is_set():
        loop_start = time.time()
        cpu_stats = get_cpu_times(connection)
        cpu_stats_percpu = get_cpu_times_percpu(connection) if percpu_filename is not None else {}
        disk_stats = get_disk_usage(connection)
        mem_stats = get_memory_usage(connection)
        swap_stats = get_swap_usage(connection)
        loadavg = get_loadavg(connection)
        net_stats = get_network_usage(connection)

        cpu_usage = 0.0
        cpu_user = 0.0
        cpu_system = 0.0
        cpu_iowait = 0.0
        cpu_steal = 0.0
        if last_cpu_stats is not None:
            # cpu_stats: user,nice,system,idle,iowait,irq,softirq,steal
            dt = sum(cpu_stats) - sum(last_cpu_stats)
            if dt > 0:
                didle = (cpu_stats[3] + cpu_stats[4]) - (last_cpu_stats[3] + last_cpu_stats[4])
                cpu_usage = 1.0 - (didle / dt)
                cpu_user = ((cpu_stats[0] + cpu_stats[1]) - (last_cpu_stats[0] + last_cpu_stats[1])) / dt
                cpu_system = ((cpu_stats[2] + cpu_stats[5] + cpu_stats[6]) - (last_cpu_stats[2] + last_cpu_stats[5] + last_cpu_stats[6])) / dt
                cpu_iowait = (cpu_stats[4] - last_cpu_stats[4]) / dt
                cpu_steal = (cpu_stats[7] - last_cpu_stats[7]) / dt

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

        container_pct: str = ""
        if docker_container:
            pct = get_docker_stats_cpu_pct(connection, docker_container)
            if pct is not None:
                container_pct = f"{pct:.6f}"

        with open(filename, "a") as f:
            ts_epoch = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(
                f"{ts_epoch:.3f},{ts},{cpu_usage},{mem_stats[0]},{mem_stats[1]},{mem_stats[2]},"
                f"{swap_stats[0]},{swap_stats[1]},{swap_stats[2]},"
                f"{loadavg[0]},{loadavg[1]},{loadavg[2]},"
                f"{cpu_user},{cpu_system},{cpu_iowait},{cpu_steal},"
                f"{disk_reads},{disk_writes},{net_rx},{net_tx},{container_pct}\n"
            )

        # Optional: per-CPU utilization series.
        if percpu_filename is not None and cpu_stats_percpu:
            if not percpu_cpus:
                percpu_cpus = sorted(cpu_stats_percpu.keys())
                with open(percpu_filename, "w") as pf:
                    pf.write("ts_epoch_s,ts," + ",".join(f"{c}_usage_ratio" for c in percpu_cpus) + "\n")
            ratios: list[str] = []
            if last_cpu_stats_percpu is not None:
                for c in percpu_cpus:
                    cur = cpu_stats_percpu.get(c)
                    prev = last_cpu_stats_percpu.get(c) if last_cpu_stats_percpu else None
                    r = ""
                    if cur is not None and prev is not None:
                        dt = sum(cur) - sum(prev)
                        if dt > 0:
                            didle = (cur[3] + cur[4]) - (prev[3] + prev[4])
                            r = str(1.0 - (didle / dt))
                    ratios.append(r)
            else:
                ratios = ["" for _ in percpu_cpus]
            with open(percpu_filename, "a") as pf:
                pf.write(f"{ts_epoch:.3f},{ts}," + ",".join(ratios) + "\n")

        last_cpu_stats = cpu_stats
        last_cpu_stats_percpu = cpu_stats_percpu or last_cpu_stats_percpu
        last_disk_stats = disk_stats
        last_net_stats = net_stats

        time_to_sleep = interval - (time.time() - loop_start)
        if time_to_sleep > 0:
            time.sleep(time_to_sleep)
    connection.close()


def capture_socket_queues(
    sample_dir: pathlib.Path,
    host: str,
    logger: logging.Logger,
    stop_event,
    *,
    ports: list[int],
    interval: int = 5,
    out_csv: pathlib.Path | None = None,
) -> None:
    """
    Capture OS-level TCP listen queue depths (Recv-Q/Send-Q) for the given ports.

    This approximates "queued requests" at each tier when the bottleneck is accept/backlog.
    """
    filename = out_csv or (sample_dir / "socket_queues.csv")
    with open(filename, "w") as f:
        f.write("ts_epoch_s,ts,port,recv_q,send_q\n")

    connection = Connection(host)
    while not stop_event.is_set():
        loop_start = time.time()
        ts_epoch = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        for port in ports:
            recv_q = -1
            send_q = -1
            try:
                # Example output columns: State Recv-Q Send-Q Local:Port Peer:Port
                out = connection.run(f"ss -ltnH 'sport = :{int(port)}' || true", hide=True, warn=True)
                line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
                if line:
                    parts = line.split()
                    if len(parts) >= 3:
                        recv_q = int(parts[1])
                        send_q = int(parts[2])
            except Exception:
                pass

            with open(filename, "a") as f:
                f.write(f"{ts_epoch:.3f},{ts},{int(port)},{recv_q},{send_q}\n")

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
    "resolve_remote_primary_ipv4",
    "resolve_remote_preferred_ipv4",
    "save_image_tar",
    "scp_from_remote",
    "scp_to_remote",
    "ssh",
    "ssh_warmup",
    "wait_for_remote_http",
    "capture_host_performance",
    "capture_socket_queues",
    "get_cpu_times",
    "get_loadavg",
    "get_swap_usage",
]


import hashlib
import logging
import pathlib
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Tuple
from fabric import Connection

import docker
import requests

from env.base import Env

_docker_client = docker.from_env()

_REMOTE_LOAD_PACKAGES = ("locust", "faker", "zope.event==5")
_REMOTE_ENV_MARKER = hashlib.sha256(
    "|".join(_REMOTE_LOAD_PACKAGES).encode("utf-8")
).hexdigest()[:12]


@dataclass
class RemoteConfig:
    app_host: str
    app_private_addr: str | None
    load_host: str
    remote_base_dir: str
    app_port: int | None = None
    max_startup_wait: float | None = None
    poll_interval: float = 2.0
    request_timeout: float = 5.0

    def __post_init__(self) -> None:
        if not self.app_host:
            raise ValueError("Remote bench requires an app host")
        if not self.load_host:
            raise ValueError("Remote bench requires a load host")

    def remote_dir(self, *parts: str) -> str:
        base = self.remote_base_dir.rstrip("/")
        if not base:
            base = "."
        suffix = "/".join(filter(None, parts))
        if not suffix:
            return base
        return f"{base}/{suffix}"


def _ensure_remote_python_env(
    load_host: str,
    remote_env_dir: str,
    logger: logging.Logger,
) -> str:
    env_path = pathlib.PurePosixPath(remote_env_dir)
    parent = str(env_path.parent)
    marker = str(env_path / f".setup-{_REMOTE_ENV_MARKER}")
    activate = str(env_path / "bin" / "activate")
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
        # f". {shlex.quote(activate)}; "
        "python -m pip install --upgrade pip; "
        f"python -m pip install {requirements}; "
        # "deactivate >/dev/null 2>&1 || true; "
        f"touch {shlex.quote(marker)}; "
        "fi"
    )

    _ssh(load_host, setup_cmd, logger)
    return locust_bin


def _run_subprocess(
    cmd: list[str],
    logger: logging.Logger,
    cwd: pathlib.Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    logger.debug("Running command: %s", " ".join(shlex.quote(x) for x in cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
    logger.debug("Command finished with code %s", result.returncode)
    if result.stdout:
        logger.debug("Command output:\n%s", result.stdout.decode(errors="ignore"))
    # result.check_returncode()
    return result


def _ssh(
    host: str,
    command: str,
    logger: logging.Logger,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    ssh_cmd = ["ssh", host, command]
    return _run_subprocess(ssh_cmd, logger, timeout=timeout)


def _scp_to_remote(
    local_path: pathlib.Path,
    host: str,
    remote_path: str,
    logger: logging.Logger,
) -> None:
    scp_cmd = ["scp", str(local_path), f"{host}:{remote_path}"]
    _run_subprocess(scp_cmd, logger)


def _scp_from_remote(
    host: str,
    remote_path: str,
    local_path: pathlib.Path,
    logger: logging.Logger,
) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    scp_cmd = ["scp", f"{host}:{remote_path}", str(local_path)]
    _run_subprocess(scp_cmd, logger)


def _save_image_tar(
    image_id: str,
    sample_dir: pathlib.Path,
    logger: logging.Logger,
) -> pathlib.Path:
    tar_path = sample_dir / f"{image_id[7:][:12]}.tar"
    if tar_path.exists():
        logger.info("Reusing existing docker image tar at %s", tar_path)
        return tar_path

    logger.info("Saving docker image %s to %s", image_id, tar_path)
    image = _docker_client.images.get(image_id)
    with open(tar_path, "wb") as f:
        for chunk in image.save(named=True):
            f.write(chunk)
    return tar_path


def _wait_for_remote_http(
    host: str,
    port: int,
    config: RemoteConfig,
    env: Env,
    logger: logging.Logger,
) -> None:
    wait_budget = config.max_startup_wait or env.wait_to_start_time
    start = time.time()
    last_exc: Exception | None = None
    while time.time() - start < wait_budget:
        try:
            probe_cmd = (
                "set -euo pipefail; "
                f"curl {host}:{port} --max-time 5"
            )
            probe_cmd = f'bash -lc "{probe_cmd}"'

            out = _ssh(config.load_host, probe_cmd, logger)
            out.check_returncode()
            logger.info("Remote server %s:%d is ready", host,port)
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            logger.info("Remote server not ready yet: %s", exc)
        time.sleep(config.poll_interval)
    raise TimeoutError(
        f"Remote server {host}:{port} did not respond within {wait_budget} seconds"
    ) from last_exc


def _get_cpu_usage(connection: Connection) -> Tuple[int, int]:
    """
    Returns the number of overall cycles, and idle cpu cycles. To get cpu usage, record these values repeatedly, and
    use the delta between the executions to compute actual cpu usage.
    """
    cmd = "cat /proc/stat | grep '^cpu '"

    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1

    parts = out.stdout.split()
    # cpu user nice system idle iowait irq softirq steal guest guest_nice ...
    nums = list(map(int, parts[1:]))
    user, nice, system, idle, iowait, irq, softirq, steal, *_ = nums + [0] * (9 - len(nums))
    idle_all = idle + iowait
    non_idle = user + nice + system + irq + softirq + steal
    total = idle_all + non_idle

    return total, idle_all


def _get_memory_usage(connection: Connection) -> Tuple[float, float, float]:
    """
    Return (used, total, used_percent) memory from /proc/meminfo.
    """
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
            meminfo[key] = int(value)  # in kB
        except ValueError:
            pass

    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", 0)

    used = total - available
    used_pct = (used / total * 100.0) if total > 0 else 0.0

    # Convert to MB
    total_mb = total / 1024.0
    used_mb = used / 1024.0
    return used_mb, total_mb, used_pct


def _get_disk_usage(connection: Connection, disk: str = "sda") -> Tuple[int, int]:
    """
    Read /proc/diskstats for the given device and return read/written sectors. Use deltas of two consecutive runs to
    get number of reads in the interval.
    See 'man iostat' or kernel docs for /proc/diskstats format.
    """
    cmd = f"cat /proc/diskstats | awk '$3==\"{disk}\" {{print $6, $10}}'"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1

    parts = out.stdout.split()
    if len(parts) < 2:
        return -1, -1

    read_sectors = int(parts[0])
    written_sectors = int(parts[1])
    return read_sectors, written_sectors


def _get_network_usage(connection: Connection) -> Tuple[int, int]:
    cmd = "cat /proc/net/dev"
    out = connection.run(cmd, hide=True)
    if not out.ok:
        return -1, -1

    lines = out.stdout.strip().splitlines()
    bytes_rx = 0
    bytes_tx = 0
    for line in lines[2:]:
        iface, data = line.split(":", 1)
        stats = data.split()
        bytes_rx += int(stats[0])
        bytes_tx += int(stats[8])

    return bytes_rx, bytes_tx


def capture_host_performance(sample_dir: pathlib.Path, host: str, logger: logging.Logger, stop_event, interval: int=1) -> None:
    # write the csv header
    filename = sample_dir / f"server_performance.csv"
    with open(filename, "w") as f:
        f.write("timestamp,cpu_usage,mem_used_mbytes,mem_free_mbytes,disk_read_bps,disk_write_bps,network_rx_bytes,network_tx_bytes\n")

    connection = Connection(host)

    last_cpu_stats = None
    last_disk_stats = None
    last_net_stats = None
    while not stop_event.is_set():
        loop_start = time.time()
        cpu_stats = _get_cpu_usage(connection)
        disk_stats = _get_disk_usage(connection)
        mem_stats = _get_memory_usage(connection)
        net_stats = _get_network_usage(connection)

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
            time.sleep(1)

    connection.close()


def run_remote_bench(
    config: RemoteConfig,
    env: Env,
    sample_slug: str,
    sample_dir: pathlib.Path,
    image_id: str,
    locustfile: pathlib.Path,
    csv_prefix: pathlib.Path,
    timeout: int,
    logger: logging.Logger,
) -> None:
    app_host = config.app_host
    load_host = config.load_host
    app_port = config.app_port or env.port
    app_private_addr = config.app_private_addr or app_host

    remote_app_dir = config.remote_dir("app", sample_slug)
    remote_load_dir = config.remote_dir("load", sample_slug) 
    remote_env_dir = '~/.local'
    # remote_env_dir = config.remote_dir("load", ".venv")

    container_name = f"baxbench-{sample_slug}-{uuid.uuid4().hex[:8]}"

    tar_path = _save_image_tar(image_id, sample_dir, logger)
    remote_tar = f"{remote_app_dir}/{tar_path.name}"

    logger.info(
        "Using remote hosts app=%s (private=%s) load=%s, container=%s, port=%d",
        app_host,
        app_private_addr,
        load_host,
        container_name,
        app_port,
    )

    out = _ssh(app_host, f"mkdir -p {shlex.quote(remote_app_dir)}", logger)
    out.check_returncode()

    out = _ssh(app_host, f"test -f {remote_tar}", logger)
    if out.returncode != 0:
        _scp_to_remote(tar_path, app_host, remote_tar, logger)

    start_cmd = (
        "set -euo pipefail; "
        f"cd {shlex.quote(remote_app_dir)}; "
        f"docker rm -f {shlex.quote(container_name)} >/dev/null 2>&1 || true; "
        f"docker load -i {shlex.quote(tar_path.name)} >/dev/null; "
        f"docker run -d --name {shlex.quote(container_name)} "
        f"-p {app_port}:{env.port}/tcp {shlex.quote(image_id)}"
    )
    start_cmd = f'bash -lc "{start_cmd}"'

    _ssh(app_host, start_cmd, logger)

    try:
        _wait_for_remote_http(app_private_addr, app_port, config, env, logger)

        _ssh(load_host, f"mkdir -p {shlex.quote(remote_load_dir)}", logger)
        remote_locustfile = f"{remote_load_dir}/{locustfile.name}"
        _scp_to_remote(locustfile, load_host, remote_locustfile, logger)

        # prepare the performance logging thread
        metrics_capture_stop_event = threading.Event()
        metrics_capture_thread = threading.Thread(target=capture_host_performance, args=(sample_dir, app_host, logger, metrics_capture_stop_event), daemon=True)
        connection = Connection(load_host)

        locust_bin = _ensure_remote_python_env(load_host, remote_env_dir, logger)
        remote_csv_prefix = f"{remote_load_dir}/{csv_prefix.name}"
        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(remote_load_dir)}; "
            f"{locust_bin} --headless --locustfile {shlex.quote(locustfile.name)} "
            # f"{shlex.quote(locust_bin)} --headless --locustfile {shlex.quote(locustfile.name)} "
            f"--host http://{app_private_addr}:{app_port} "
            "--users 21600 "
            "--spawn-rate 120 "
            "--run-time 3m "
            f"--csv {shlex.quote(csv_prefix.name)} "
            "--csv-full-history "
            "--only-summary "
            "--processes -1"
        )

        metrics_capture_thread.start()
        locust_proc = connection.run(locust_cmd, hide=True)
        metrics_capture_stop_event.set()
        metrics_capture_thread.join()

        logger.info("Locust output:\n%s", locust_proc)
        connection.close()

        for suffix in ("_stats_history.csv", "_stats.csv", "_failures.csv", "_exceptions.csv"):
            remote_csv = f"{remote_csv_prefix}{suffix}"
            local_csv = pathlib.Path(f"{csv_prefix}{suffix}")
            try:
                _scp_from_remote(load_host, remote_csv, local_csv, logger)
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "Failed to copy remote CSV %s: %s", remote_csv, exc
                )
    finally:
        stop_cmd = f"docker rm -f {shlex.quote(container_name)} >/dev/null 2>&1 || true"
        stop_cmd = f'bash -lc "{stop_cmd}"'
        try:
            _ssh(app_host, stop_cmd, logger)
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to cleanup remote container: %s", exc)

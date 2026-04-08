import hashlib
import logging
import os
import pathlib
import shlex
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
import concurrent.futures
import socket
import re
from dataclasses import dataclass
from typing import Tuple
from fabric import Connection

import docker
import requests

from env.base import Env
from db_manager import PostgresManager

# _docker_client = docker.from_env()

_REMOTE_LOAD_PACKAGES = ("locust", "faker", "zope.event==5")
_REMOTE_ENV_MARKER = hashlib.sha256(
    "|".join(_REMOTE_LOAD_PACKAGES).encode("utf-8")
).hexdigest()[:12]

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


def _ssh_control_path(host: str) -> str:
    """
    Path for SSH ControlMaster socket (kept short to avoid UNIX path limits).
    """
    base = os.environ.get(
        "BAXBENCH_SSH_CONTROL_DIR", os.path.join(tempfile.gettempdir(), "baxbench-ssh")
    )
    # Create the directory lazily; ignore errors (SSH will fail with a clear message if unusable).
    try:
        pathlib.Path(base).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    hid = hashlib.sha256(host.encode("utf-8")).hexdigest()[:10]
    return os.path.join(base, f"cm-{hid}")


def _ssh_base_cmd(host: str) -> list[str]:
    cmd = ["ssh"]
    if _SSH_MULTIPLEX:
        cp = _ssh_control_path(host)
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


def _scp_base_cmd(host: str) -> list[str]:
    cmd = ["scp"]
    if _SSH_MULTIPLEX:
        cp = _ssh_control_path(host)
        cmd += ["-o", f"ControlPath={cp}"]
    return cmd


def _collect_docker_logs_bundle(
    host: str,
    bundle_name: str,
    container_names: list[str],
    remote_base_dir: str,
    local_out_dir: pathlib.Path,
    logger: logging.Logger,
) -> None:
    """
    Collect docker logs on `host` for `container_names`, tar them on the host, scp back,
    and extract into local_out_dir/bundle_name/.
    """
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
        # Best-effort: do not fail the whole run if a container is missing.
        logs_cmd_parts.append(
            f"(docker logs --timestamps {shlex.quote(cname)} > {shlex.quote(remote_dir)}/{shlex.quote(cname)}.log 2>&1 || true);"
        )
        logs_cmd_parts.append(
            f"(docker inspect {shlex.quote(cname)} > {shlex.quote(remote_dir)}/{shlex.quote(cname)}.inspect.json 2>&1 || true);"
        )
    logs_cmd_parts.append(
        f"tar -czf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_base_dir.rstrip('/'))} {shlex.quote(bundle_name)};"
    )

    _ssh(host, f"bash -lc {shlex.quote(' '.join(logs_cmd_parts))}", logger)
    local_tgz = local_bundle_dir / f"{bundle_name}.tgz"
    _scp_from_remote(host, remote_tgz, local_tgz, logger)

    try:
        with tarfile.open(local_tgz, "r:gz") as tf:
            tf.extractall(path=local_bundle_dir)
    except Exception as exc:
        logger.warning("Failed to extract docker logs bundle %s from %s: %s", bundle_name, host, exc)

    # Best-effort remote cleanup
    _ssh(host, f"bash -lc {shlex.quote(f'rm -rf {remote_dir} {remote_tgz} || true')}", logger)


@dataclass
class RemoteConfig:
    app_host: str
    load_host: str
    remote_base_dir: str
    app_private_addr: str | None = None
    app_hosts: list[str] | None = None
    app_port: int | None = None
    lb_host: str | None = None
    db_host: str | None = None
    max_startup_wait: float | None = None
    poll_interval: float = 0.5
    request_timeout: float = 2.0

    def __post_init__(self) -> None:
        if not self.app_host and not self.app_hosts:
            raise ValueError("Remote bench requires an app host or app hosts")
        if not self.load_host:
            raise ValueError("Remote bench requires a load host")

        if self.app_hosts is not None and len(self.app_hosts) == 0:
            raise ValueError("--bench-app-hosts must not be empty when provided")

    def selected_backend_hosts(self) -> list[str]:
        hosts = self.app_hosts if self.app_hosts is not None else [self.app_host]
        if not hosts:
            return []
        # Multi-backend mode deploys one backend per host in --bench-app-hosts.
        return list(hosts)

    def selected_db_host(self) -> str:
        if self.db_host:
            return self.db_host
        backends = self.selected_backend_hosts()
        if not backends:
            raise ValueError("No backend hosts available to select a db host")
        return backends[0]

    def selected_lb_host(self) -> str:
        return self.lb_host or self.load_host

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

    _ssh(load_host, f"bash -lc {shlex.quote(setup_cmd)}", logger)
    return locust_bin


def _run_subprocess(
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
    # result.check_returncode()
    return result


def _ssh(
    host: str,
    command: str,
    logger: logging.Logger,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    ssh_cmd = _ssh_base_cmd(host) + [command]
    return _run_subprocess(ssh_cmd, logger, timeout=timeout)


def _ensure_rootless_docker(host: str, logger: logging.Logger) -> None:
    """
    Best-effort: ensure the user's rootless Docker daemon is running.

    This is intended for environments where `docker` uses a user socket like
    unix:///run/user/<uid>/docker.sock.
    """
    cmd = (
        "set -euo pipefail; "
        # Keep user services (incl. rootless docker) alive after logout when allowed.
        # This is idempotent; on locked-down systems it may fail with permission errors.
        "if command -v loginctl >/dev/null 2>&1; then "
        "  loginctl enable-linger \"$USER\" >/dev/null 2>&1 || true; "
        "fi; "
        # If systemd user services exist, this is idempotent.
        "if command -v systemctl >/dev/null 2>&1; then "
        "  systemctl --user is-active docker >/dev/null 2>&1 || systemctl --user start docker >/dev/null 2>&1 || true; "
        "fi; "
        # Quick sanity check (non-fatal; the caller will fail later with clearer logs if still broken).
        "docker info >/dev/null 2>&1 || true"
    )
    _ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)


def _scp_to_remote(
    local_path: pathlib.Path,
    host: str,
    remote_path: str,
    logger: logging.Logger,
) -> None:
    scp_cmd = _scp_base_cmd(host) + [str(local_path), f"{host}:{remote_path}"]
    _run_subprocess(scp_cmd, logger)


def _scp_from_remote(
    host: str,
    remote_path: str,
    local_path: pathlib.Path,
    logger: logging.Logger,
) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    scp_cmd = _scp_base_cmd(host) + [f"{host}:{remote_path}", str(local_path)]
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
    client = docker.from_env()
    image = client.images.get(image_id)
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
    probe_host: str | None = None,
) -> None:
    wait_budget = config.max_startup_wait or env.wait_to_start_time
    start = time.time()
    last_exc: Exception | None = None
    while time.time() - start < wait_budget:
        try:
            probe_cmd = (
                "set -euo pipefail; "
                # Probe reachability (TCP connectivity), not strict HTTP success.
                # Some apps may return 404/405 on `/` even when healthy.
                f"curl -sS -o /dev/null http://{host}:{port}/ --max-time 5"
            )
            probe_cmd = f'bash -lc "{probe_cmd}"'

            out = _ssh(probe_host or config.load_host, probe_cmd, logger)
            out.check_returncode()
            logger.info("Remote server %s:%d is ready", host, port)
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            logger.info("Remote server not ready yet: %s", exc)
        time.sleep(config.poll_interval)
    raise TimeoutError(
        f"Remote server {host}:{port} did not respond within {wait_budget} seconds"
    ) from last_exc


def _resolve_ipv4(hostname: str) -> str:
    """
    Resolve `hostname` to an IPv4 address on the orchestrator machine.
    Containers/rootless DNS may not be able to resolve short hostnames reliably,
    so we use IPs for network-context values (DB_HOST, nginx upstreams, curl probes).
    """
    try:
        info = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
        # getaddrinfo returns tuples: (family, type, proto, canonname, sockaddr)
        return info[0][4][0]
    except socket.gaierror as exc:
        raise ValueError(
            f"Unable to resolve '{hostname}' to an IPv4 address on the orchestrator. "
            "Pass a DNS-resolvable hostname or an explicit IP."
        ) from exc


def _resolve_remote_primary_ipv4(host: str, logger: logging.Logger) -> str:
    """
    Resolve an IPv4 address from the *remote host's* perspective.

    This is used when the orchestrator resolves a hostname to a loopback address
    (e.g. 127.0.1.1 via /etc/hosts), which is not usable from within containers.
    """
    # Use the source address the host would use to reach the public internet.
    cmd = (
        "set -euo pipefail; "
        # iproute2 is present on Ubuntu; fall back to hostname -I if needed.
        "ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i==\"src\") {print $(i+1); exit}}' "
        "|| hostname -I | awk '{print $1}'"
    )
    out = _ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)
    out.check_returncode()
    text = (out.stdout or b"").decode(errors="ignore")
    # SSH to these hosts prints a login banner; extract the actual IPv4 from output.
    ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    ip = ips[-1].strip() if ips else ""
    if not ip or ip.startswith("127."):
        raise ValueError(
            f"Unable to determine a non-loopback IPv4 for remote host {host!r}; got output {text!r}"
        )
    return ip


def _start_remote_ssh_tunnel(
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
    """
    Start a persistent local SSH tunnel on `host` and return pidfile path.
    Tunnel listens on `bind_host`:`local_port` on `host`.
    """
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
    _ssh(host, f"bash -lc {shlex.quote(cmd)}", logger).check_returncode()
    return pidfile


def _stop_remote_ssh_tunnel(
    host: str,
    pidfile: str,
    logger: logging.Logger,
) -> None:
    cmd = (
        "set -euo pipefail; "
        f"if [ -f {shlex.quote(pidfile)} ]; then "
        f"  kill $(cat {shlex.quote(pidfile)}) >/dev/null 2>&1 || true; "
        f"  rm -f {shlex.quote(pidfile)}; "
        "fi"
    )
    _ssh(host, f"bash -lc {shlex.quote(cmd)}", logger)


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
    # todo: get as percentage of full
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


def capture_host_performance(
    sample_dir: pathlib.Path,
    host: str,
    logger: logging.Logger,
    stop_event,
    interval: int = 10,
) -> None:
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
            time.sleep(time_to_sleep)

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
    needs_db: bool = False,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
) -> None:
    # Use current remote defaults if not already provided
    users = str(bench_users) if bench_users is not None else "7200"
    spawn_rate = str(bench_spawn_rate) if bench_spawn_rate is not None else "40"
    run_time_s = int(bench_run_time) if bench_run_time is not None else 180
    run_time = f"{run_time_s}s"

    backend_hosts = config.selected_backend_hosts()
    if not backend_hosts:
        raise ValueError("No backend hosts selected for remote benchmarking")

    load_host = config.load_host
    lb_host = config.selected_lb_host()
    db_host = config.selected_db_host() if needs_db else ""
    # IMPORTANT: backend_hosts/lb_host/db_host are used in network contexts (URLs, nginx upstream, DB_HOST).
    # Do not pass SSH user@host strings here. Use ~/.ssh/config aliases (hostnames) if you need a fixed user.
    bad = [h for h in ([*backend_hosts, lb_host] + ([db_host] if needs_db else [])) if "@" in h]
    if bad:
        raise ValueError(
            "Remote multi-host bench requires network-reachable hostnames without 'user@'. "
            f"Got: {bad}. "
            "Recommendation: add host aliases in ~/.ssh/config (with User dscherre) and use those aliases here."
        )

    app_port = config.app_port or env.port
    # For single-host legacy mode, allow overriding the reachable address used by the load host.
    # For multi-host mode, backend hosts are used directly in the LB upstream list.
    app_private_addr = config.app_private_addr or (backend_hosts[0] if backend_hosts else "")

    # One app dir per backend host (avoids collisions and enables parallel runs later)
    def _host_slug(h: str) -> str:
        return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in h)

    remote_app_dirs: dict[str, str] = {
        h: config.remote_dir("app", sample_slug, _host_slug(h)) for h in backend_hosts
    }
    remote_load_dir = config.remote_dir("load", sample_slug) 
    remote_env_dir = config.remote_dir("load", ".venv")

    container_name = f"baxbench-{sample_slug}-{uuid.uuid4().hex[:8]}"
    db_container_name = container_name + "-db"
    # Use a stable LB name per sample so we can:
    # - reliably replace any prior debug LB still holding the port
    # - make it easier to find logs on the LB host after a run
    lb_container_name = f"baxbench-{sample_slug}-lb"

    tar_path = _save_image_tar(image_id, sample_dir, logger)
    remote_tars: dict[str, str] = {
        h: f"{remote_app_dirs[h]}/{tar_path.name}" for h in backend_hosts
    }

    logger.info(
        "Using remote hosts backends=%s db=%s lb=%s load=%s, container=%s, port=%d",
        ",".join(backend_hosts),
        db_host if needs_db else "(none)",
        lb_host,
        load_host,
        container_name,
        app_port,
    )

    # Resolve network addresses (IP) so container DNS failures don't break DB_HOST/nginx/upstream/probes.
    backend_net_hosts: dict[str, str] = {h: _resolve_ipv4(h) for h in backend_hosts}
    lb_net_host = _resolve_ipv4(lb_host)
    db_net_host = _resolve_ipv4(db_host) if needs_db else ""
    remote_tunnel_dir = config.remote_dir("tunnels", sample_slug)
    active_tunnels: list[tuple[str, str]] = []
    active_lb_tunnels: list[tuple[str, str]] = []

    # Rootless docker should be running on every involved host.
    involved_hosts = sorted(set([*backend_hosts, lb_host] + ([db_host] if needs_db else [])))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(involved_hosts) or 1)) as ex:
        list(ex.map(lambda h: _ensure_rootless_docker(h, logger), involved_hosts))

    # Best-effort pre-cleanup of per-sample directories on each host.
    # This prevents leftover artifacts from previous runs (or partial failures) from breaking setup.
    def _preclean_host(h: str) -> None:
        paths: list[str] = []
        if h in remote_app_dirs:
            paths.append(remote_app_dirs[h])
        if h == lb_host:
            paths.append(config.remote_dir("lb", sample_slug))
            paths.append(config.remote_dir("tunnels", sample_slug))
        if h == load_host:
            paths.append(remote_load_dir)
        if needs_db and h == db_host:
            paths.append(config.remote_dir("db", sample_slug))

        if not paths:
            return
        rm_cmd = "set -euo pipefail; " + " ".join(
            f"rm -rf {shlex.quote(p)};" for p in paths
        )
        # Ignore failures: permission issues will surface on the subsequent mkdir/copy steps with
        # clearer context (and may require admin intervention if the base dir is not writable).
        _ssh(h, f"bash -lc {shlex.quote(rm_cmd)}", logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(involved_hosts) or 1)) as ex:
        list(ex.map(_preclean_host, involved_hosts))

    # Ensure dirs + copy image tar to all backend hosts
    def _prep_backend_dir_and_tar(h: str) -> None:
        out = _ssh(h, f"mkdir -p {shlex.quote(remote_app_dirs[h])}", logger)
        out.check_returncode()
        out = _ssh(h, f"test -f {shlex.quote(remote_tars[h])}", logger)
        if out.returncode != 0:
            _scp_to_remote(tar_path, h, remote_tars[h], logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(backend_hosts) or 1)) as ex:
        list(ex.map(_prep_backend_dir_and_tar, backend_hosts))

    # Environment variables passed to backend containers
    env_vars_base = f"-e PORT={env.port} "
    db_host_for_backend: dict[str, str] = {}
    db_port_for_backend: dict[str, int] = {}
    
    # if needs db, start postgres container
    if needs_db:
        # Start Postgres on db_host, published on 5432 for cross-host access.
        # NOTE: this assumes backend hosts can reach db_host:5432.
        start_db_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {shlex.quote(db_container_name)} >/dev/null 2>&1 || true; "
            f"docker run -d --name {shlex.quote(db_container_name)} "
            # Bind explicitly so other hosts can reach it (rootless may otherwise bind to loopback).
            "-p 0.0.0.0:5432:5432 "
            f"-e POSTGRES_USER={PostgresManager.DEFAULT_USER} "
            f"-e POSTGRES_PASSWORD={PostgresManager.DEFAULT_PASSWORD} "
            f"-e POSTGRES_DB={PostgresManager.DEFAULT_DATABASE} "
            f"postgres:17-alpine "
            "-c listen_addresses='*' "
            "-c max_connections=300 "
            "-c shared_preload_libraries=pg_stat_statements "
            "-c pg_stat_statements.track=all "
            "-c track_io_timing=on"
        )
        start_db_cmd = f'bash -lc "{start_db_cmd}"'
        _ssh(db_host, start_db_cmd, logger)
        logger.info(f"Remote Postgres started")
        
        # Wait for DB ready
        wait_cmd = (
             f"timeout 30s bash -lc 'until docker exec {shlex.quote(db_container_name)} pg_isready -U {PostgresManager.DEFAULT_USER}; do sleep 1; done'"
        )
        _ssh(db_host, wait_cmd, logger)

        # Enable pg_stat_statements extension (best-effort)
        sql = "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
        ext_cmd = (
            "set -euo pipefail; "
            f"docker exec {shlex.quote(db_container_name)} "
            f"psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} "
            "-v ON_ERROR_STOP=1 "
            f"-c {shlex.quote(sql)}"
        )
        _ssh(db_host, f"bash -lc {shlex.quote(ext_cmd)}", logger)

        # Backends connect to DB through host-bound SSH tunnels when they are not on db_host.
        # Containers cannot use host loopback directly, so they target the host IP.
        for idx, h in enumerate(backend_hosts):
            if h == db_host:
                db_host_for_backend[h] = db_net_host
                db_port_for_backend[h] = 5432
                continue
            local_db_port = 15432 + idx
            pidfile = _start_remote_ssh_tunnel(
                host=h,
                tunnel_name=f"db-{sample_slug}-{idx}",
                local_port=local_db_port,
                target_host="127.0.0.1",
                target_port=5432,
                ssh_dest=db_host,
                tunnel_dir=remote_tunnel_dir,
                logger=logger,
                bind_host="0.0.0.0",
            )
            active_tunnels.append((h, pidfile))
            db_host_for_backend[h] = backend_net_hosts[h]
            db_port_for_backend[h] = local_db_port

    # Start one backend container per host
    backend_container_names: dict[str, str] = {}
    for h in backend_hosts:
        backend_container_names[h] = f"{container_name}-app-{_host_slug(h)}"

    def _start_backend(h: str) -> None:
        cname = backend_container_names[h]
        env_vars = env_vars_base
        if needs_db:
            env_vars += (
                f"-e DB_HOST={shlex.quote(db_host_for_backend[h])} "
                f"-e DB_PORT={db_port_for_backend[h]} "
                f"-e DB_USER={PostgresManager.DEFAULT_USER} "
                f"-e DB_PASSWORD={PostgresManager.DEFAULT_PASSWORD} "
                f"-e DB_NAME={PostgresManager.DEFAULT_DATABASE} "
            )
        start_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(remote_app_dirs[h])}; "
            f"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true; "
            f"docker load -i {shlex.quote(tar_path.name)} >/dev/null; "
            f"docker run -d --name {shlex.quote(cname)} "
            f"{env_vars} "
            # Bind explicitly so other hosts can reach it (rootless may otherwise bind to loopback).
            f"-p 0.0.0.0:{app_port}:{env.port}/tcp {shlex.quote(image_id)}"
        )
        _ssh(h, f'bash -lc "{start_cmd}"', logger)

    # Start backends in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(backend_hosts) or 1)) as ex:
        list(ex.map(_start_backend, backend_hosts))

    # Give containers a moment to start and potentially fail
    time.sleep(2)

    # Check backend logs (best-effort)
    def _fetch_backend_logs(h: str) -> tuple[str, bytes]:
        cname = backend_container_names[h]
        logs_cmd = f'bash -lc "docker logs {shlex.quote(cname)} 2>&1 | tail -n 200"'
        logs_result = _ssh(h, logs_cmd, logger)
        return (h, logs_result.stdout or b"")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(backend_hosts) or 1)) as ex:
        for h, out in ex.map(_fetch_backend_logs, backend_hosts):
            if out:
                logger.info("Backend logs (%s):\n%s", h, out)

    # DB sampler state (must exist for finally even if we fail early)
    remote_db_csv = ""
    remote_db_stop = ""
    remote_db_pid = ""

    try:
        # Wait for each backend to be reachable on its own host loopback.
        for h in backend_hosts:
            _wait_for_remote_http("127.0.0.1", app_port, config, env, logger, probe_host=h)

        # Build LB upstream endpoints; use ssh tunnels so we don't depend on inter-host published ports.
        #
        # IMPORTANT: Nginx runs inside a Docker container on lb_host. That means `127.0.0.1` from Nginx
        # points to the container loopback, not the host loopback where the SSH tunnel binds.
        # So we bind tunnels on 0.0.0.0 and let Nginx reach them via the host's routable IP.
        lb_host_ip = lb_net_host
        if lb_host_ip.startswith("127."):
            lb_host_ip = _resolve_remote_primary_ipv4(lb_host, logger)
        lb_upstream_endpoints: list[tuple[str, int]] = []
        for idx, h in enumerate(backend_hosts):
            local_lb_port = 17001 + idx
            pidfile = _start_remote_ssh_tunnel(
                host=lb_host,
                tunnel_name=f"lb-{sample_slug}-{idx}",
                local_port=local_lb_port,
                target_host="127.0.0.1",
                target_port=app_port,
                ssh_dest=h,
                tunnel_dir=remote_tunnel_dir,
                logger=logger,
                bind_host="0.0.0.0",
            )
            active_tunnels.append((lb_host, pidfile))
            active_lb_tunnels.append((lb_host, pidfile))
            lb_upstream_endpoints.append((lb_host_ip, local_lb_port))

        # Start nginx LB on lb_host (can be the load host).
        upstream = "\n".join(
            f"        server {host}:{port};" for host, port in lb_upstream_endpoints
        )
        nginx_conf = (
            "events {}\n"
            "http {\n"
            "    upstream baxbench_upstream {\n"
            f"{upstream}\n"
            "    }\n"
            "    server {\n"
            "        listen 80;\n"
            "        location / {\n"
            "            proxy_pass http://baxbench_upstream;\n"
            "            proxy_http_version 1.1;\n"
            "            proxy_set_header Connection \"\";\n"
            "            proxy_set_header Host $host;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        remote_lb_dir = config.remote_dir("lb", sample_slug)
        remote_nginx_conf = f"{remote_lb_dir}/nginx.conf"
        _ssh(lb_host, f"mkdir -p {shlex.quote(remote_lb_dir)}", logger).check_returncode()
        # Write nginx.conf on lb host
        write_cmd = f"cat > {shlex.quote(remote_nginx_conf)} <<'EOF'\n{nginx_conf}\nEOF\n"
        _ssh(lb_host, f"bash -lc {shlex.quote(write_cmd)}", logger)

        lb_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {shlex.quote(lb_container_name)} >/dev/null 2>&1 || true; "
            f"docker run -d --name {shlex.quote(lb_container_name)} "
            # Publish the LB on app_port. Rootless Docker often does not support `--network host`.
            f"-p 0.0.0.0:{app_port}:80 "
            f"-v {shlex.quote(remote_nginx_conf)}:/etc/nginx/nginx.conf:ro "
            "nginx:1.27-alpine"
        )
        _ssh(lb_host, f'bash -lc "{lb_cmd}"', logger)

        # Wait for LB to be reachable from load host.
        lb_target_for_load = "127.0.0.1" if load_host == lb_host else lb_net_host
        _wait_for_remote_http(lb_target_for_load, app_port, config, env, logger)

        # Start DB metrics sampler on DB host (best-effort) to produce db_performance.csv.
        # This matches the local db_performance.csv schema used by plotting.
        if needs_db:
            remote_db_dir = config.remote_dir("db", sample_slug)
            remote_db_csv = f"{remote_db_dir}/db_performance.csv"
            remote_db_stop = f"{remote_db_dir}/STOP"
            remote_db_pid = f"{remote_db_dir}/sampler.pid"
            _ssh(db_host, f"mkdir -p {shlex.quote(remote_db_dir)}", logger).check_returncode()
            sampler_cmd = (
                "set -euo pipefail; "
                f"rm -f {shlex.quote(remote_db_stop)} {shlex.quote(remote_db_pid)}; "
                f"echo \"ts,numbackends,xact_commit,xact_rollback,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time_ms,blk_write_time_ms,stmt_calls,stmt_total_exec_time_ms\" > {shlex.quote(remote_db_csv)}; "
                "(\n"
                f"  while [ ! -f {shlex.quote(remote_db_stop)} ]; do\n"
                "    ts=$(date +%s);\n"
                f"    dbrow=$(docker exec {shlex.quote(db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT numbackends,xact_commit,xact_rollback,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time,blk_write_time FROM pg_stat_database WHERE datname = current_database();\" | tail -n 1 | tr -d '\\r');\n"
                f"    stmtrow=$(docker exec {shlex.quote(db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT COALESCE(SUM(calls),0),COALESCE(SUM(total_exec_time),0) FROM pg_stat_statements;\" | tail -n 1 | tr -d '\\r');\n"
                f"    echo \"${{ts}}.${{RANDOM}},${{dbrow}},${{stmtrow}}\" >> {shlex.quote(remote_db_csv)};\n"
                "    sleep 1;\n"
                "  done\n"
                ") >/dev/null 2>&1 & echo $! > "
                f"{shlex.quote(remote_db_pid)}"
            )
            _ssh(db_host, f"bash -lc {shlex.quote(sampler_cmd)}", logger)

        _ssh(load_host, f"mkdir -p {shlex.quote(remote_load_dir)}", logger)
        remote_locustfile = f"{remote_load_dir}/{locustfile.name}"
        _scp_to_remote(locustfile, load_host, remote_locustfile, logger)

        # prepare the performance logging thread
        metrics_capture_stop_event = threading.Event()
        # Capture performance on the LB host (default == load host), since this machine now fronts all requests.
        metrics_capture_thread = threading.Thread(
            target=capture_host_performance,
            args=(sample_dir, lb_host, logger, metrics_capture_stop_event),
            daemon=True,
        )
        connection = Connection(load_host)

        locust_bin = _ensure_remote_python_env(load_host, remote_env_dir, logger)
        remote_csv_prefix = f"{remote_load_dir}/{csv_prefix.name}"
        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(remote_load_dir)}; "
            f"{locust_bin} --headless --locustfile {shlex.quote(locustfile.name)} "
            # f"{shlex.quote(locust_bin)} --headless --locustfile {shlex.quote(locustfile.name)} "
            f"--host http://{lb_target_for_load}:{app_port} "
            f"--users {users} "
            f"--spawn-rate {spawn_rate} "
            f"--run-time {run_time} "
            f"--csv {shlex.quote(csv_prefix.name)} "
            "--csv-full-history "
            "--only-summary "
        )

        metrics_capture_thread.start()
        locust_proc = connection.run(locust_cmd, hide=True, warn=True)
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

        # Stop DB sampler and fetch db_performance.csv
        if needs_db and remote_db_csv:
            try:
                _ssh(db_host, f"bash -lc \"touch {shlex.quote(remote_db_stop)} || true\"", logger)
                # Best-effort wait
                _ssh(
                    db_host,
                    f"bash -lc \"if [ -f {shlex.quote(remote_db_pid)} ]; then kill -0 $(cat {shlex.quote(remote_db_pid)}) >/dev/null 2>&1 || true; fi\"",
                    logger,
                )
                _scp_from_remote(
                    db_host,
                    remote_db_csv,
                    sample_dir / "db_performance.csv",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to fetch db_performance.csv: %s", exc)
    finally:
        # Collect container logs into sample_dir/logs (best-effort).
        # This runs even if teardown is skipped to preserve debugging evidence.
        if _COLLECT_DOCKER_LOGS:
            try:
                logs_out = pathlib.Path(sample_dir) / "logs"
                # LB bundle
                _collect_docker_logs_bundle(
                    host=lb_host,
                    bundle_name=f"lb-{sample_slug}",
                    container_names=[lb_container_name],
                    remote_base_dir=config.remote_dir("logs", sample_slug),
                    local_out_dir=logs_out,
                    logger=logger,
                )
                # Backend bundles (one per backend host)
                for h, cname in backend_container_names.items():
                    _collect_docker_logs_bundle(
                        host=h,
                        bundle_name=f"app-{sample_slug}-{_host_slug(h)}",
                        container_names=[cname],
                        remote_base_dir=config.remote_dir("logs", sample_slug),
                        local_out_dir=logs_out,
                        logger=logger,
                    )
                # DB bundle
                if needs_db:
                    _collect_docker_logs_bundle(
                        host=db_host,
                        bundle_name=f"db-{sample_slug}",
                        container_names=[db_container_name],
                        remote_base_dir=config.remote_dir("logs", sample_slug),
                        local_out_dir=logs_out,
                        logger=logger,
                    )
            except Exception as exc:
                logger.warning("Failed to collect docker logs bundles: %s", exc)

        # Optional: skip *all* teardown to enable post-run debugging on the remote hosts.
        # This intentionally leaves SSH tunnels, containers (LB/backends/DB), and remote dirs in place.
        skip_teardown = (
            os.environ.get("BAXBENCH_SKIP_TEARDOWN", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if skip_teardown:
            logger.info(
                "Skipping all remote teardown (BAXBENCH_SKIP_TEARDOWN=1). "
                "Leaving tunnels/containers running for debugging."
            )
            return

        # Cleanup ssh tunnels.
        for tunnel_host, pidfile in active_tunnels:
            try:
                _stop_remote_ssh_tunnel(tunnel_host, pidfile, logger)
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup tunnel %s on %s: %s", pidfile, tunnel_host, exc)

        # Cleanup LB
        keep_lb = (os.environ.get("BAXBENCH_KEEP_LB", "").strip().lower() in ("1", "true", "yes", "on"))
        if keep_lb:
            logger.info("Keeping load balancer container %s on %s (BAXBENCH_KEEP_LB=1)", lb_container_name, lb_host)
        else:
            try:
                _ssh(
                    lb_host,
                    f"bash -lc \"docker rm -f {shlex.quote(lb_container_name)} >/dev/null 2>&1 || true\"",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup LB container: %s", exc)

        # Cleanup backends
        for h, cname in backend_container_names.items():
            try:
                _ssh(
                    h,
                    f"bash -lc \"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true\"",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup backend %s: %s", h, exc)

        # Cleanup DB
        if needs_db:
            try:
                # Stop DB sampler if still running
                if remote_db_stop:
                    _ssh(db_host, f"bash -lc \"touch {shlex.quote(remote_db_stop)} || true\"", logger)
                _ssh(
                    db_host,
                    f"bash -lc \"docker rm -f {shlex.quote(db_container_name)} >/dev/null 2>&1 || true\"",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup DB container: %s", exc)

import hashlib
import logging
import pathlib
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass

import docker
import requests

from env.base import Env

_docker_client = docker.from_env()

_REMOTE_LOAD_PACKAGES = ("locust", "faker")
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
        f". {shlex.quote(activate)}; "
        "python -m pip install --upgrade pip; "
        f"python -m pip install {requirements}; "
        "deactivate >/dev/null 2>&1 || true; "
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
            response = requests.get(
                f"http://{host}:{port}",
                timeout=config.request_timeout,
            )
            logger.info(
                "Remote server %s:%d responded with %d",
                host,
                port,
                response.status_code,
            )
            return
        except requests.RequestException as exc:
            last_exc = exc
            logger.debug("Remote server not ready yet: %s", exc)
        time.sleep(config.poll_interval)
    raise TimeoutError(
        f"Remote server {host}:{port} did not respond within {wait_budget} seconds"
    ) from last_exc


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
    remote_env_dir = config.remote_dir("load", ".venv")

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

    _ssh(app_host, f"mkdir -p {shlex.quote(remote_app_dir)}", logger)
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

        locust_bin = _ensure_remote_python_env(load_host, remote_env_dir, logger)
        remote_csv_prefix = f"{remote_load_dir}/{csv_prefix.name}"
        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(remote_load_dir)}; "
            f"{shlex.quote(locust_bin)} --headless --locustfile {shlex.quote(locustfile.name)} "
            f"--host http://{app_private_addr}:{app_port} "
            f"--csv {shlex.quote(csv_prefix.name)} "
            "--csv-full-history "
            "--only-summary"
        )

        locust_proc = _ssh(load_host, locust_cmd, logger, timeout=timeout)
        logger.info("Locust output:\n%s", locust_proc.stdout.decode(errors="ignore"))

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

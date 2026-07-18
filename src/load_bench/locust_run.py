"""
Remote Locust execution on SSH load hosts (master + workers), used by k8s-bench.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fabric import Connection

import remote_exec
from bench_models import host_slug

from .paths import locust_dir, locust_logs_dir
from .load_profiles import LoadProfile
from .load_profiles.manifest import LOAD_PROFILE_MANIFEST_FILENAME
from .load_topology import LoadTopology

_BAXBENCH_SHAPE = Path(__file__).resolve().parent / "load_profiles" / "_baxbench_shape.py"
_BAXBENCH_SHAPES_DIR = Path(__file__).resolve().parent / "load_profiles" / "shapes"


# --- Local run-dir helpers (k8s copies locustfile before remote staging) ---


def resolve_locust_user_class(locustfile: Path, requested: str = "default") -> str:
    if requested and requested != "default":
        return requested
    text = locustfile.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"class\s+(\w+)\s*\(\s*(?:Fast)?HttpUser\s*\)", text)
    if match:
        return match.group(1)
    return requested


def _stage_baxbench_shapes(staging: Path) -> None:
    """Copy shape facade + ``shapes/`` package beside the locustfile."""
    if _BAXBENCH_SHAPE.is_file():
        shutil.copy2(_BAXBENCH_SHAPE, staging / "_baxbench_shape.py")
    if not _BAXBENCH_SHAPES_DIR.is_dir():
        return
    dest = staging / "shapes"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        _BAXBENCH_SHAPES_DIR,
        dest,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "_monolith*",
            "*.py.py",
        ),
    )


def prepare_locust_run_dir(
    run_dir: Path,
    locustfile: Path,
    *,
    load_profile: LoadProfile | None = None,
    bench_run_time_s: int | None = None,
    bench_users: int | None = None,
) -> Path:
    """Stage the locustfile + shape under ``<run_dir>/locust/``."""
    from .load_profiles.manifest import write_load_profile_manifest

    staging = locust_dir(run_dir)
    dest_locust = staging / locustfile.name
    if locustfile.resolve() != dest_locust.resolve():
        shutil.copy2(locustfile, dest_locust)
    _stage_baxbench_shapes(staging)
    if load_profile is not None and bench_run_time_s is not None:
        write_load_profile_manifest(
            staging / LOAD_PROFILE_MANIFEST_FILENAME,
            load_profile,
            bench_run_time_s=int(bench_run_time_s),
            bench_users=bench_users,
        )
    return dest_locust


# --- Remote Locust run config + classes ---


@dataclass(frozen=True)
class DistributedLocustConfig:
    topology: LoadTopology
    locustfile: Path
    csv_prefix: Path
    remote_load_dir: str
    remote_env_dir: str
    app_port: int
    bench_users: int
    bench_spawn_rate: int
    bench_run_time_s: int
    locust_run_time: str
    load_profile: LoadProfile
    load_taskset_cpus: str | None = None
    target_base_url: str | None = None
    sample_dir: Path | None = None
    sample_slug: str = "run"
    logger: logging.Logger | None = None

    @property
    def logs_dir(self) -> Path:
        """``<run_dir>/locust/logs/`` for the bench run that owns this config."""
        base = self.sample_dir or self.csv_prefix.parent.parent.parent
        return locust_logs_dir(base)


class _LocustStaging:
    def __init__(self, config: DistributedLocustConfig) -> None:
        self._cfg = config

    def deploy(self) -> None:
        log = self._cfg.logger or logging.getLogger(__name__)
        cfg = self._cfg

        def _one(host: str) -> None:
            remote_exec.ssh(host, f"mkdir -p {shlex.quote(cfg.remote_load_dir)}", log).check_returncode()
            remote_locustfile = f"{cfg.remote_load_dir}/{cfg.locustfile.name}"
            remote_exec.scp_to_remote(cfg.locustfile, host, remote_locustfile, log)
            staged_shape = cfg.locustfile.parent / "_baxbench_shape.py"
            staged_shapes = cfg.locustfile.parent / "shapes"
            if staged_shape.is_file():
                remote_exec.scp_to_remote(
                    staged_shape,
                    host,
                    f"{cfg.remote_load_dir}/_baxbench_shape.py",
                    log,
                )
            elif _BAXBENCH_SHAPE.is_file():
                remote_exec.scp_to_remote(
                    _BAXBENCH_SHAPE,
                    host,
                    f"{cfg.remote_load_dir}/_baxbench_shape.py",
                    log,
                )
            shapes_dir = staged_shapes if staged_shapes.is_dir() else _BAXBENCH_SHAPES_DIR
            if shapes_dir.is_dir():
                remote_exec.scp_tree_to_remote(
                    shapes_dir,
                    host,
                    f"{cfg.remote_load_dir}/shapes",
                    log,
                )
            manifest = cfg.locustfile.parent / LOAD_PROFILE_MANIFEST_FILENAME
            if manifest.is_file():
                remote_exec.scp_to_remote(
                    manifest,
                    host,
                    f"{cfg.remote_load_dir}/{LOAD_PROFILE_MANIFEST_FILENAME}",
                    log,
                )
            remote_exec.ensure_remote_python_env(host, cfg.remote_env_dir, log)

        hosts = self._cfg.topology.all_hosts
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(hosts) or 1)) as ex:
            list(ex.map(_one, hosts))


class LocustWorker:
    """Locust worker process on a remote host, connected to the master."""

    def __init__(
        self,
        config: DistributedLocustConfig,
        *,
        host: str,
        index: int,
        master_ip: str,
        master_port: int,
    ) -> None:
        self._cfg = config
        self.host = host
        self.index = index
        self._master_ip = master_ip
        self._master_port = master_port
        slug = host_slug(host)
        base = config.remote_load_dir
        self.pidfile = f"{base}/locust-worker-{slug}-{index}.pid"
        self.logfile = f"{base}/locust-worker-{slug}-{index}.log"

    def start(self) -> None:
        log = self._cfg.logger or logging.getLogger(__name__)
        cfg = self._cfg
        locust_bin = remote_exec.ensure_remote_python_env(self.host, cfg.remote_env_dir, log)
        ts = cfg.load_taskset_cpus
        worker_exec = (
            f"nohup taskset -c {shlex.quote(ts)} {shlex.quote(locust_bin)} "
            if ts
            else f"nohup {shlex.quote(locust_bin)} "
        )
        w_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(cfg.remote_load_dir)}; "
            f"rm -f {shlex.quote(self.pidfile)} {shlex.quote(self.logfile)} || true; "
            f"{worker_exec}"
            f"--worker --master-host {shlex.quote(self._master_ip)} "
            f"--master-port {int(self._master_port)} "
            f"--locustfile {shlex.quote(cfg.locustfile.name)} "
            f"> {shlex.quote(self.logfile)} 2>&1 & "
            f"echo $! > {shlex.quote(self.pidfile)}"
        )
        remote_exec.ssh(self.host, f"bash -lc {shlex.quote(w_cmd)}", log).check_returncode()

    def stop(self) -> None:
        log = self._cfg.logger or logging.getLogger(__name__)
        kill_cmd = (
            "set -euo pipefail; "
            f"if [ -f {shlex.quote(self.pidfile)} ]; then "
            f"  kill $(cat {shlex.quote(self.pidfile)}) >/dev/null 2>&1 || true; "
            f"  rm -f {shlex.quote(self.pidfile)}; "
            "fi"
        )
        remote_exec.ssh(self.host, f"bash -lc {shlex.quote(kill_cmd)}", log)

    def fetch_log(self, logs_dir: Path) -> None:
        log = self._cfg.logger or logging.getLogger(__name__)
        try:
            remote_exec.scp_from_remote(
                self.host,
                self.logfile,
                logs_dir / f"worker-{host_slug(self.host)}-{self.index}.log",
                log,
            )
        except Exception as exc:
            log.warning("Failed to fetch worker log from %s: %s", self.host, exc)


class LocustMaster:
    """Locust master (headless, distributed mode); aggregates CSV results."""

    def __init__(self, config: DistributedLocustConfig) -> None:
        self._cfg = config

    @staticmethod
    def _stable_port(base: int, key: str, span: int = 4000) -> int:
        hid = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
        return base + (hid % span)

    def resolve_bind(self) -> tuple[str, int]:
        cfg = self._cfg
        master_ip = remote_exec.resolve_remote_preferred_ipv4(
            cfg.topology.master,
            cfg.logger or logging.getLogger(__name__),
        )
        master_port = self._stable_port(15557, f"locust-master:{cfg.sample_slug}")
        return master_ip, master_port

    def run(
        self,
        *,
        target: str,
        expect_workers: int,
        master_bind_ip: str,
        master_bind_port: int,
    ) -> tuple[str, object, str]:
        cfg = self._cfg
        log = cfg.logger or logging.getLogger(__name__)
        master_host = cfg.topology.master

        master_args = (
            f"--master --headless "
            f"--master-bind-host 0.0.0.0 --master-bind-port {int(master_bind_port)} "
            f"--expect-workers {int(expect_workers)} --expect-workers-max-wait 60 "
        )

        locust_host = cfg.target_base_url or f"http://{target}:{cfg.app_port}"
        target_log = locust_host

        locust_bin = remote_exec.ensure_remote_python_env(master_host, cfg.remote_env_dir, log)
        load_ts = cfg.load_taskset_cpus
        locust_exec = f"{shlex.quote(locust_bin)}"
        if load_ts:
            locust_exec = f"taskset -c {shlex.quote(load_ts)} {shlex.quote(locust_bin)}"

        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(cfg.remote_load_dir)}; "
            f"{locust_exec} {master_args}"
            f"--locustfile {shlex.quote(cfg.locustfile.name)} "
            f"--host {shlex.quote(locust_host)} "
            f"--users {int(cfg.bench_users)} "
            f"--spawn-rate {int(cfg.bench_spawn_rate)} "
            f"--run-time {cfg.locust_run_time} "
            f"--csv {shlex.quote(cfg.csv_prefix.name)} "
            "--only-summary "
        )

        connection = Connection(master_host)
        locust_proc = connection.run(locust_cmd, hide=True, warn=True)
        log.info("Locust master output (%s):\n%s", master_host, locust_proc)
        connection.close()
        return locust_cmd, locust_proc, target_log

    def write_loader_log(
        self,
        *,
        logs_dir: Path,
        locust_cmd: str,
        locust_proc: object,
        target_log: str,
        master_ip: str,
        master_port: int,
        worker_hosts: list[str],
    ) -> None:
        master_host = self._cfg.topology.master
        loader_log_path = logs_dir / f"master-{host_slug(master_host)}.log"
        try:
            loader_log_path.write_text(
                "## mode: distributed\n"
                f"## master_host: {master_host}\n"
                f"## master_ip: {master_ip}\n"
                f"## master_port: {master_port}\n"
                f"## workers: {', '.join(worker_hosts) or '(none)'}\n"
                f"## target: {target_log}\n"
                f"## cmd: {locust_cmd}\n\n"
                f"--- stdout ---\n{getattr(locust_proc, 'stdout', '') or ''}\n\n"
                f"--- stderr ---\n{getattr(locust_proc, 'stderr', '') or ''}\n",
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as exc:
            (self._cfg.logger or logging.getLogger(__name__)).warning(
                "Failed to write loader log for %s: %s", master_host, exc
            )

    def fetch_csvs(self) -> None:
        cfg = self._cfg
        log = cfg.logger or logging.getLogger(__name__)
        master_host = cfg.topology.master
        remote_csv_prefix = f"{cfg.remote_load_dir}/{cfg.csv_prefix.name}"
        for suffix in ("_stats_history.csv", "_stats.csv", "_failures.csv", "_exceptions.csv"):
            remote_csv = f"{remote_csv_prefix}{suffix}"
            local_csv = Path(f"{cfg.csv_prefix}{suffix}")
            try:
                remote_exec.scp_from_remote(master_host, remote_csv, local_csv, log)
            except subprocess.CalledProcessError as exc:
                log.warning("Failed to copy remote CSV %s from %s: %s", suffix, master_host, exc)


class DistributedLocustSession:
    """Stage files, start workers, run master, collect logs and CSVs."""

    def __init__(self, config: DistributedLocustConfig) -> None:
        self._cfg = config

    def run(self, *, target: str) -> None:
        cfg = self._cfg
        topology = cfg.topology
        worker_hosts = list(topology.workers)

        _LocustStaging(cfg).deploy()

        locust_master = LocustMaster(cfg)
        master_bind_ip, master_bind_port = locust_master.resolve_bind()

        workers: list[LocustWorker] = []
        for i, wh in enumerate(worker_hosts):
            w = LocustWorker(
                cfg,
                host=wh,
                index=i,
                master_ip=master_bind_ip,
                master_port=master_bind_port,
            )
            w.start()
            workers.append(w)

        logs_dir = cfg.logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)

        locust_cmd, locust_proc, target_log = locust_master.run(
            target=target,
            expect_workers=len(worker_hosts),
            master_bind_ip=master_bind_ip,
            master_bind_port=master_bind_port,
        )
        locust_master.write_loader_log(
            logs_dir=logs_dir,
            locust_cmd=locust_cmd,
            locust_proc=locust_proc,
            target_log=target_log,
            master_ip=master_bind_ip,
            master_port=master_bind_port,
            worker_hosts=worker_hosts,
        )

        for w in workers:
            w.fetch_log(logs_dir)
        locust_master.fetch_csvs()
        for w in workers:
            w.stop()

from __future__ import annotations

import concurrent.futures
import hashlib
import pathlib
import shlex
import subprocess
import threading

from fabric import Connection

import remote_exec
from bench_models import DistributedBenchContext, host_slug

from .config import RuntimeToggles
from .load_profiles import ContinuousLoadProfile, LoadProfile, SpikeLoadProfile, StairsLoadProfile
from .system_configs import SystemTopology


class LocustRunner:
    def __init__(
        self,
        *,
        ctx: DistributedBenchContext,
        toggles: RuntimeToggles,
        load_profile: LoadProfile,
        system_topology: SystemTopology,
    ):
        self.ctx = ctx
        self.toggles = toggles
        self.load_profile = load_profile
        self.system_topology = system_topology
        self.plan = ctx.plan
        self.logger = ctx.logger

    @staticmethod
    def _stable_port(base: int, key: str, span: int = 4000) -> int:
        hid = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
        return base + (hid % span)

    def run(self, *, target: str) -> None:
        """
        Run Locust from one or more load generator hosts.

        target is the target host (IP/hostname) that Locust should send load to.
        """
        master_host = self.plan.load_master
        worker_hosts = list(self.plan.load_workers)
        load_hosts_unique = sorted(set([master_host, *worker_hosts]))
        if not master_host:
            raise ValueError("No load master host configured for LocustRunner.run")

        metrics_capture_stop_event = threading.Event()
        perf_threads: list[threading.Thread] = []
        queue_threads: list[threading.Thread] = []

        # Building ordered list of hosts to capture metrics from.
        perf_hosts = (
            load_hosts_unique
            + list(self.plan.backend_hosts)
            + ([self.plan.db_hosts[0]] if self.plan.needs_db else [])
        )
        if self.plan.lb_host:
            perf_hosts.append(self.plan.lb_host)
        seen: set[str] = set()
        perf_hosts = [h for h in perf_hosts if not (h in seen or seen.add(h))]

        def _docker_container_for_host(h: str) -> str | None:
            if h in self.ctx.backend_container_names:
                return self.ctx.backend_container_names[h]
            if self.plan.needs_db and h == self.plan.db_hosts[0]:
                return self.ctx.db_container_name
            if h == self.plan.lb_host:
                return self.ctx.lb_container_name
            return None

        for host in perf_hosts:
            host_stats_dir = self.ctx.sample_dir / "stats" / host_slug(host)
            host_stats_dir.mkdir(parents=True, exist_ok=True)
            out_csv = host_stats_dir / "host_performance.csv"
            t = threading.Thread(
                target=remote_exec.capture_host_performance,
                args=(self.ctx.sample_dir, host, self.logger, metrics_capture_stop_event),
                kwargs={
                    "out_csv": out_csv,
                    "interval": 5,
                    "docker_container": _docker_container_for_host(host),
                },
                daemon=True,
            )
            perf_threads.append(t)

            ports: list[int] = []
            if host in self.plan.backend_hosts or host == self.plan.lb_host:
                ports.append(int(self.plan.app_port))
            if self.plan.needs_db and host == self.plan.db_hosts[0]:
                ports.append(5432)
            if ports:
                q_csv = host_stats_dir / "socket_queue.csv"
                qt = threading.Thread(
                    target=remote_exec.capture_socket_queues,
                    args=(self.ctx.sample_dir, host, self.logger, metrics_capture_stop_event),
                    kwargs={"ports": ports, "out_csv": q_csv, "interval": 5},
                    daemon=True,
                )
                queue_threads.append(qt)

        # Stage locustfile and env on all load hosts first.
        def _stage_one_load_host(h: str) -> None:
            remote_exec.ssh(h, f"mkdir -p {shlex.quote(self.ctx.remote_load_dir)}", self.logger).check_returncode()
            remote_locustfile = f"{self.ctx.remote_load_dir}/{self.ctx.locustfile.name}"
            remote_exec.scp_to_remote(self.ctx.locustfile, h, remote_locustfile, self.logger)
            # Shared load-shaping helper; locustfiles import this by filename from the load dir.
            local_shape = pathlib.Path(__file__).parent / "load_profiles" / "_baxbench_shape.py"
            if local_shape.is_file():
                remote_exec.scp_to_remote(
                    local_shape,
                    h,
                    f"{self.ctx.remote_load_dir}/_baxbench_shape.py",
                    self.logger,
                )
            remote_exec.ensure_remote_python_env(h, self.ctx.remote_env_dir, self.logger)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(load_hosts_unique) or 1)) as ex:
            list(ex.map(_stage_one_load_host, load_hosts_unique))

        locust_processes = max(1, int(self.load_profile.locust_processes))
        is_distributed = bool(worker_hosts)
        # Locust disallows combining distributed master mode with multiprocessing mode.
        # Keep --processes for single-host runs only.
        locust_processes_eff = 1 if is_distributed else locust_processes
        locust_processes_arg = f"--processes {locust_processes_eff} " if locust_processes_eff > 1 else ""
        locust_csv_full_history = "--csv-full-history " if locust_processes_eff <= 1 else ""

        for t in perf_threads:
            t.start()
        for t in queue_threads:
            t.start()

        logs_dir = self.ctx.sample_dir / "logs" / f"loader-{self.ctx.sample_slug}"
        logs_dir.mkdir(parents=True, exist_ok=True)

        worker_pidfiles: dict[str, str] = {}
        worker_logfiles: dict[str, str] = {}
        master_ip = ""
        master_port = 0
        expect_workers = 0

        if isinstance(self.load_profile, ContinuousLoadProfile):
            load_mode = "continuous"
        elif isinstance(self.load_profile, StairsLoadProfile):
            load_mode = "stairs"
        elif isinstance(self.load_profile, SpikeLoadProfile):
            load_mode = "spike"
        else:
            load_mode = "steady"
        env_prefix = (
            f"BAXBENCH_LOCUST_WAIT_MIN_S={self.load_profile.wait_min_s} "
            f"BAXBENCH_LOCUST_WAIT_MAX_S={self.load_profile.wait_max_s} "
            f"BAXBENCH_LOAD_MODE={shlex.quote(load_mode)} "
            f"BAXBENCH_RUN_TIME_S={int(self.plan.bench_run_time_s)} "
        )
        if load_mode == "steady":
            env_prefix += f"BAXBENCH_STEADY_USERS={int(self.plan.bench_users)} "
        elif load_mode == "continuous":
            if isinstance(self.load_profile, ContinuousLoadProfile):
                env_prefix += (
                    f"BAXBENCH_CONTINUOUS_SPAWN_RATE={int(self.load_profile.spawn_rate)} "
                    f"BAXBENCH_CONTINUOUS_START_USERS={int(self.load_profile.start_users)} "
                    f"BAXBENCH_CONTINUOUS_TARGET_USERS={int(self.load_profile.target_users)} "
                )
        elif load_mode == "stairs":
            if isinstance(self.load_profile, StairsLoadProfile):
                env_prefix += (
                    f"BAXBENCH_STAIRS_START_USERS={int(self.load_profile.start_users)} "
                    f"BAXBENCH_STAIRS_STEP_USERS={int(self.load_profile.step_users)} "
                    f"BAXBENCH_STAIRS_STEP_DURATION_S={int(self.load_profile.step_duration_s)} "
                    f"BAXBENCH_STAIRS_STEPS={int(self.load_profile.steps)} "
                )
        elif load_mode == "spike":
            if isinstance(self.load_profile, SpikeLoadProfile):
                env_prefix += (
                    f"BAXBENCH_SPIKE_BASE_USERS={int(self.load_profile.base_users)} "
                    f"BAXBENCH_SPIKE_USERS={int(self.load_profile.spike_users)} "
                    f"BAXBENCH_SPIKE_INTERVAL_S={int(self.load_profile.interval_s)} "
                    f"BAXBENCH_SPIKE_DURATION_S={int(self.load_profile.duration_s)} "
                )

        if is_distributed:
            # Distributed master/worker mode.
            master_ip = remote_exec.resolve_remote_preferred_ipv4(
                master_host, self.logger, preferred_prefixes=("10.233.",)
            )
            master_port = self._stable_port(15557, f"locust-master:{self.ctx.sample_slug}")
            expect_workers = len(worker_hosts)

            # Start workers in background (nohup) on each worker host.
            for i, wh in enumerate(worker_hosts):
                worker_key = f"{wh}#{i}"
                pidfile = f"{self.ctx.remote_load_dir}/locust-worker-{host_slug(wh)}-{i}.pid"
                logfile = f"{self.ctx.remote_load_dir}/locust-worker-{host_slug(wh)}-{i}.log"
                worker_pidfiles[worker_key] = pidfile
                worker_logfiles[worker_key] = logfile
                locust_bin = remote_exec.ensure_remote_python_env(wh, self.ctx.remote_env_dir, self.logger)
                ts = self.system_topology.load_resources.taskset_cpus
                worker_exec = (
                    # Don't use VAR=... cmd "$VAR" under `set -u` (the "$VAR" expands before the env assignment).
                    f"nohup taskset -c {shlex.quote(ts)} {shlex.quote(locust_bin)} "
                    if ts
                    else f"nohup {shlex.quote(locust_bin)} "
                )
                w_cmd = (
                    "set -euo pipefail; "
                    f"cd {shlex.quote(self.ctx.remote_load_dir)}; "
                    f"rm -f {shlex.quote(pidfile)} {shlex.quote(logfile)} || true; "
                    f"{env_prefix}{worker_exec}"
                    f"--worker --master-host {shlex.quote(master_ip)} --master-port {int(master_port)} "
                    f"--locustfile {shlex.quote(self.ctx.locustfile.name)} "
                    f"> {shlex.quote(logfile)} 2>&1 & "
                    f"echo $! > {shlex.quote(pidfile)}"
                )
                remote_exec.ssh(wh, f"bash -lc {shlex.quote(w_cmd)}", self.logger).check_returncode()

        # Run master in foreground for completion + CSVs.
        connection = Connection(master_host)
        locust_bin = remote_exec.ensure_remote_python_env(master_host, self.ctx.remote_env_dir, self.logger)

        load_ts = self.system_topology.load_resources.taskset_cpus
        locust_exec = f"{shlex.quote(locust_bin)}"
        if load_ts:
            locust_exec = f"taskset -c {shlex.quote(load_ts)} {shlex.quote(locust_bin)}"

        distributed_master_args = (
            f"--master --headless "
            f"--master-bind-host 0.0.0.0 --master-bind-port {int(master_port)} "
            f"--expect-workers {int(expect_workers)} --expect-workers-max-wait 60 "
        ) if is_distributed else "--headless "

        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(self.ctx.remote_load_dir)}; "
            f"{env_prefix}{locust_exec} {distributed_master_args}"
            f"--locustfile {shlex.quote(self.ctx.locustfile.name)} "
            f"--host http://{target}:{self.plan.app_port} "
            f"--users {int(self.plan.bench_users)} "
            f"--spawn-rate {int(self.plan.bench_spawn_rate)} "
            f"--run-time {self.plan.locust_run_time} "
            f"{locust_processes_arg}"
            f"--csv {shlex.quote(self.ctx.csv_prefix.name)} "
            f"{locust_csv_full_history}"
            "--only-summary "
        )

        locust_proc = connection.run(locust_cmd, hide=True, warn=True)
        self.logger.info(
            "Locust %s output (%s):\n%s",
            "distributed master" if is_distributed else "single loader",
            master_host,
            locust_proc,
        )
        connection.close()

        loader_log_path = (
            logs_dir / f"locust-master-{host_slug(master_host)}.log"
            if is_distributed
            else logs_dir / f"locust-{host_slug(master_host)}.log"
        )
        loader_log_mode = "distributed" if is_distributed else "single"
        loader_log_extra = (
            f"## master_host: {master_host}\n"
            f"## master_ip: {master_ip}\n"
            f"## master_port: {master_port}\n"
            f"## workers: {', '.join(worker_hosts)}\n"
            if is_distributed
            else f"## load_host: {master_host}\n"
        )
        try:
            loader_log_path.write_text(
                f"## mode: {loader_log_mode}\n"
                f"{loader_log_extra}"
                f"## target: http://{target}:{self.plan.app_port}\n"
                f"## cmd: {locust_cmd}\n\n"
                f"--- stdout ---\n{getattr(locust_proc, 'stdout', '') or ''}\n\n"
                f"--- stderr ---\n{getattr(locust_proc, 'stderr', '') or ''}\n",
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as exc:
            self.logger.warning("Failed to write loader log for %s: %s", master_host, exc)

        if is_distributed:
            # Fetch worker logs
            for i, wh in enumerate(worker_hosts):
                worker_key = f"{wh}#{i}"
                try:
                    remote_exec.scp_from_remote(
                        wh,
                        worker_logfiles[worker_key],
                        (logs_dir / f"locust-worker-{host_slug(wh)}-{i}.log"),
                        self.logger,
                    )
                except Exception as exc:
                    self.logger.warning("Failed to fetch worker log from %s: %s", wh, exc)

        # Copy CSVs back from the host that aggregates results (master).
        remote_csv_prefix = f"{self.ctx.remote_load_dir}/{self.ctx.csv_prefix.name}"
        for suffix in ("_stats_history.csv", "_stats.csv", "_failures.csv", "_exceptions.csv"):
            remote_csv = f"{remote_csv_prefix}{suffix}"
            local_csv = pathlib.Path(f"{self.ctx.csv_prefix}{suffix}")
            try:
                remote_exec.scp_from_remote(master_host, remote_csv, local_csv, self.logger)
            except subprocess.CalledProcessError as exc:
                self.logger.warning("Failed to copy remote CSV %s from %s: %s", suffix, master_host, exc)

        # Cleanup worker processes (distributed mode only).
        if is_distributed:
            for i, wh in enumerate(worker_hosts):
                worker_key = f"{wh}#{i}"
                pidfile = worker_pidfiles.get(worker_key, "")
                if not pidfile:
                    continue
                kill_cmd = (
                    "set -euo pipefail; "
                    f"if [ -f {shlex.quote(pidfile)} ]; then "
                    f"  kill $(cat {shlex.quote(pidfile)}) >/dev/null 2>&1 || true; "
                    f"  rm -f {shlex.quote(pidfile)}; "
                    "fi"
                )
                remote_exec.ssh(wh, f"bash -lc {shlex.quote(kill_cmd)}", self.logger)

        metrics_capture_stop_event.set()
        for t in perf_threads:
            t.join()
        for t in queue_threads:
            t.join()

from __future__ import annotations

import concurrent.futures
import pathlib
import shlex
import subprocess
import threading

from fabric import Connection

import remote_exec
from bench_models import DistributedBenchContext, host_slug

from .config import RuntimeToggles
from .load_profiles import LoadProfile
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

    def run(self, *, lb_target_for_load: str) -> None:
        remote_exec.ssh(self.plan.load_host, f"mkdir -p {shlex.quote(self.ctx.remote_load_dir)}", self.logger)
        remote_locustfile = f"{self.ctx.remote_load_dir}/{self.ctx.locustfile.name}"
        remote_exec.scp_to_remote(self.ctx.locustfile, self.plan.load_host, remote_locustfile, self.logger)

        metrics_capture_stop_event = threading.Event()
        perf_threads: list[threading.Thread] = []
        queue_threads: list[threading.Thread] = []
        perf_hosts = list(self.plan.backend_hosts) + ([self.plan.db_host] if self.plan.needs_db else []) + [self.plan.lb_host]
        seen: set[str] = set()
        perf_hosts = [h for h in perf_hosts if not (h in seen or seen.add(h))]

        def _docker_container_for_host(h: str) -> str | None:
            if h in self.ctx.backend_container_names:
                return self.ctx.backend_container_names[h]
            if self.plan.needs_db and h == self.plan.db_host:
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
            if self.plan.needs_db and host == self.plan.db_host:
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

        connection = Connection(self.plan.load_host)
        locust_bin = remote_exec.ensure_remote_python_env(self.plan.load_host, self.ctx.remote_env_dir, self.logger)
        remote_csv_prefix = f"{self.ctx.remote_load_dir}/{self.ctx.csv_prefix.name}"
        locust_processes = max(1, int(self.load_profile.locust_processes))
        locust_processes_arg = f"--processes {locust_processes} " if locust_processes > 1 else ""
        locust_csv_full_history = "--csv-full-history " if locust_processes <= 1 else ""
        extra_args = " ".join(self.load_profile.extra_locust_args).strip()
        if extra_args:
            extra_args += " "
        env_prefix = (
            f"BAXBENCH_LOCUST_WAIT_MIN_S={self.load_profile.wait_min_s} "
            f"BAXBENCH_LOCUST_WAIT_MAX_S={self.load_profile.wait_max_s} "
        )
        if self.system_topology.load_taskset_cpus:
            env_prefix += f"TASKSET_CPUS={shlex.quote(self.system_topology.load_taskset_cpus)} "
        locust_exec = f"{locust_bin}"
        if self.system_topology.load_taskset_cpus:
            locust_exec = f"taskset -c \"$TASKSET_CPUS\" {locust_bin}"
        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(self.ctx.remote_load_dir)}; "
            f"{env_prefix}{locust_exec} --headless --locustfile {shlex.quote(self.ctx.locustfile.name)} "
            f"--host http://{lb_target_for_load}:{self.plan.app_port} "
            f"--users {self.plan.bench_users} "
            f"--spawn-rate {self.plan.bench_spawn_rate} "
            f"--run-time {self.plan.locust_run_time} "
            f"{locust_processes_arg}"
            f"--csv {shlex.quote(self.ctx.csv_prefix.name)} "
            f"{locust_csv_full_history}"
            f"{extra_args}"
            "--only-summary "
        )

        for t in perf_threads:
            t.start()
        for t in queue_threads:
            t.start()
        locust_proc = connection.run(locust_cmd, hide=True, warn=True)
        metrics_capture_stop_event.set()
        for t in perf_threads:
            t.join()
        for t in queue_threads:
            t.join()

        self.logger.info("Locust output:\n%s", locust_proc)
        connection.close()

        def _copy_locust_csv(suffix: str) -> None:
            remote_csv = f"{remote_csv_prefix}{suffix}"
            local_csv = pathlib.Path(f"{self.ctx.csv_prefix}{suffix}")
            remote_exec.scp_from_remote(self.plan.load_host, remote_csv, local_csv, self.logger)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [
                ex.submit(_copy_locust_csv, s)
                for s in ("_stats_history.csv", "_stats.csv", "_failures.csv", "_exceptions.csv")
            ]
            for suffix, fut in zip(("_stats_history.csv", "_stats.csv", "_failures.csv", "_exceptions.csv"), futs):
                try:
                    fut.result()
                except subprocess.CalledProcessError as exc:
                    self.logger.warning("Failed to copy remote CSV %s: %s", suffix, exc)

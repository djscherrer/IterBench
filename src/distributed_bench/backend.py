from __future__ import annotations

import concurrent.futures
import shlex
import time

import remote_exec
from bench_models import DistributedBenchContext, host_slug
from db_manager import PostgresManager
from env.base import Env

from .config import RuntimeToggles
from .runtime import RemoteRuntime
from .system_configs import SystemTopology


class BackendManager:
    def __init__(
        self,
        *,
        ctx: DistributedBenchContext,
        env: Env,
        runtime: RemoteRuntime,
        toggles: RuntimeToggles,
        system_topology: SystemTopology,
    ):
        self.ctx = ctx
        self.env = env
        self.runtime = runtime
        self.toggles = toggles
        self.system_topology = system_topology
        self.plan = ctx.plan
        self.logger = ctx.logger

    def start_or_reuse(self) -> None:
        def _start_backend(host: str) -> None:
            cname = self.ctx.backend_container_names[host]
            env_vars = self.ctx.env_vars_base
            if self.plan.needs_db:
                env_vars += (
                    f"-e DB_HOST={shlex.quote(self.ctx.db_host_for_backend[host])} "
                    f"-e DB_PORT={self.ctx.db_port_for_backend[host]} "
                    f"-e DB_USER={PostgresManager.DEFAULT_USER} "
                    f"-e DB_PASSWORD={PostgresManager.DEFAULT_PASSWORD} "
                    f"-e DB_NAME={PostgresManager.DEFAULT_DATABASE} "
                )
            app_labels = {
                "baxbench.sample": self.ctx.sample_slug,
                "baxbench.role": "app",
                "baxbench.host": host_slug(host),
                "baxbench.image_id": self.ctx.image_id,
            }

            # Check if the backend container already exists and is using the correct image.
            existing_app = self.runtime.docker_ps_id(host, labels=app_labels) if self.toggles.keep_backends else ""
            if existing_app and self.runtime.docker_image_matches(host, existing_app, self.ctx.image_id):
                existing_name = self.runtime.docker_ps_name(host, labels=app_labels) or cname
                self.ctx.backend_container_names[host] = existing_name
                self.logger.info("Reusing backend on %s (BAXBENCH_KEEP_BACKENDS=1)", host)
                return

            # Ensure the backend container is removed if we're not keeping it.
            if not self.toggles.keep_backends:
                self.runtime.docker_rm_by_labels(
                    host,
                    labels={
                        "baxbench.sample": self.ctx.sample_slug,
                        "baxbench.role": "app",
                        "baxbench.host": host_slug(host),
                    },
                )

            start_cmd = (
                "set -euo pipefail; "
                f"cd {shlex.quote(self.ctx.remote_app_dirs[host])}; "
                f"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true; "
                f"if docker image inspect {shlex.quote(self.ctx.image_id)} >/dev/null 2>&1; then "
                "  :; "
                "else "
                f"  docker load -i {shlex.quote(self.ctx.tar_path.name)} >/dev/null; "
                "fi; "
                f"docker run -d --name {shlex.quote(cname)} "
                + " ".join(f"--label {shlex.quote(k + '=' + v)}" for k, v in app_labels.items())
                + " "
                f"{self.system_topology.backend_resources.docker_run_flags()} "
                f"{env_vars} "
                f"-p 0.0.0.0:{self.plan.app_port}:{self.env.port}/tcp {shlex.quote(self.ctx.image_id)}"
            )
            remote_exec.ssh(host, f'bash -lc "{start_cmd}"', self.logger)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(self.plan.backend_hosts) or 1)) as ex:
            list(ex.map(_start_backend, list(self.plan.backend_hosts)))

    def collect_recent_logs(self) -> None:
        def _fetch_backend_logs(host: str) -> tuple[str, bytes]:
            cname = self.ctx.backend_container_names[host]
            logs_cmd = f'bash -lc "docker logs {shlex.quote(cname)} 2>&1 | tail -n 200"'
            logs_result = remote_exec.ssh(host, logs_cmd, self.logger)
            return (host, logs_result.stdout or b"")

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(self.plan.backend_hosts) or 1)) as ex:
            for host, out in ex.map(_fetch_backend_logs, list(self.plan.backend_hosts)):
                if out:
                    self.logger.info("Backend logs (%s):\n%s", host, out)

    def wait_ready(self) -> None:
        for host in self.plan.backend_hosts:
            remote_exec.wait_for_remote_http("127.0.0.1", self.plan.app_port, self.plan.config, self.env, self.logger, probe_host=host)

    def graceful_start_delay(self) -> None:
        time.sleep(2)

    def cleanup(self) -> None:
        if self.toggles.keep_backends:
            self.logger.info("Keeping backend containers (BAXBENCH_KEEP_BACKENDS=1)")
            return
        for host, cname in self.ctx.backend_container_names.items():
            try:
                remote_exec.ssh(
                    host,
                    f"bash -lc \"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true\"",
                    self.logger,
                )
            except Exception as exc:
                self.logger.warning("Failed to cleanup backend %s: %s", host, exc)

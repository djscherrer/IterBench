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

    def start(self) -> None:
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

            # Always start fresh: remove any baxbench-managed backend on this host.
            self.runtime.docker_rm_by_labels(
                host,
                labels={
                    "baxbench.role": "app",
                    "baxbench.host": host_slug(host),
                },
            )

            cname_q = shlex.quote(cname)
            res = self.system_topology.backend_resources
            pin = res.bash_apply_taskset_to_container(cname_q)
            start_cmd = (
                "set -euo pipefail; "
                f"cd {shlex.quote(self.ctx.remote_app_dirs[host])}; "
                f"docker rm -f {cname_q} >/dev/null 2>&1 || true; "
                f"if docker image inspect {shlex.quote(self.ctx.image_id)} >/dev/null 2>&1; then "
                "  :; "
                "else "
                f"  docker load -i {shlex.quote(self.ctx.tar_path.name)} >/dev/null; "
                "fi; "
                f"docker run -d --name {cname_q} "
                + " ".join(f"--label {shlex.quote(k + '=' + v)}" for k, v in app_labels.items())
                + " "
                f"{res.docker_run_flags()} "
                f"{env_vars} "
                f"-p 0.0.0.0:{self.plan.app_port}:{self.env.port}/tcp {shlex.quote(self.ctx.image_id)}"
            )
            if pin:
                start_cmd += f"; {pin}"
            # IMPORTANT: fail fast if backend start failed (e.g. port already in use).
            # `remote_exec.ssh()` intentionally does not raise by default.
            # Use shlex.quote() to avoid breaking the script when it contains quotes (e.g. hostpin taskset).
            result = remote_exec.ssh(host, f"bash -lc {shlex.quote(start_cmd)}", self.logger)
            try:
                result.check_returncode()
            except Exception as exc:
                out = (result.stdout or b"").decode(errors="ignore").strip()
                if out:
                    raise RuntimeError(f"Failed to start backend container on {host}:\n{out}") from exc
                raise RuntimeError(f"Failed to start backend container on {host} (exit {result.returncode})") from exc

            # Verification: record the effective CPU affinity after pinning.
            if res.taskset_cpus:
                verify_cmd = (
                    "set -euo pipefail; "
                    # Rootless Docker: if DOCKER_HOST isn't set, prefer the per-user socket when present.
                    "docker_sock=\"/run/user/$(id -u)/docker.sock\"; "
                    "if [[ -z \"${DOCKER_HOST:-}\" && -S \"$docker_sock\" ]]; then export DOCKER_HOST=\"unix://$docker_sock\"; fi; "
                    f"pid=$(docker inspect -f '{{{{.State.Pid}}}}' {cname_q} 2>/dev/null || echo ''); "
                    f"echo \"PINVERIFY role=app host={host} container={cname} expected={res.taskset_cpus} pid=${{pid}}\"; "
                    "if [[ -n \"${pid:-}\" && \"${pid:-}\" != 0 ]]; then "
                    # Attempt to apply and capture errors loudly (common: EPERM under some policies).
                    f"  _out=$(taskset -apc {shlex.quote(res.taskset_cpus)} \"$pid\" 2>&1) && _rc=0 || _rc=$?; "
                    "  echo \"PINAPPLY rc=${_rc} out=${_out}\"; "
                    "  taskset -pc \"$pid\" 2>&1 || true; "
                    "  grep -E '^Cpus_allowed_list:' \"/proc/$pid/status\" 2>&1 || true; "
                    "fi"
                )
                vout = remote_exec.ssh(host, f"bash -lc {shlex.quote(verify_cmd)}", self.logger)
                txt = (vout.stdout or b"").decode(errors="ignore").strip()
                if txt:
                    self.logger.info("%s", txt)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(self.plan.backend_hosts) or 1)) as ex:
            list(ex.map(_start_backend, list(self.plan.backend_hosts)))

    def collect_recent_logs(self) -> None:
        def _fetch_backend_logs(host: str) -> tuple[str, bytes]:
            cname = self.ctx.backend_container_names[host]
            logs_cmd = f"bash -lc {shlex.quote(f'docker logs {shlex.quote(cname)} 2>&1 | tail -n 200')}"
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
        for host, cname in self.ctx.backend_container_names.items():
            try:
                self.runtime.docker_rm_by_labels(
                    host,
                    labels={
                        "baxbench.role": "app",
                        "baxbench.host": host_slug(host),
                    },
                )
            except Exception as exc:
                self.logger.warning("Failed to cleanup backend %s: %s", host, exc)

from __future__ import annotations

import shlex

import remote_exec
from bench_models import DistributedBenchContext, host_slug
from env.base import Env

from .config import RuntimeToggles
from .runtime import RemoteRuntime
from .system_configs import SystemTopology


class LoadBalancerManager:
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
        self.nginx_log_path_host = ""
        self.nginx_log_path_container = "/tmp/nginx_access_timing.csv"

    def _backend_endpoints(self) -> list[tuple[str, int]]:
        # Direct connectivity: LB proxies to each backend host directly.
        return [(self.ctx.backend_net_hosts[h], self.plan.app_port) for h in self.plan.backend_hosts]

    def setup(self) -> None:
        if not self.plan.lb_host:
            raise ValueError("LoadBalancerManager.setup called but lb_host is empty")
        endpoints = self._backend_endpoints()
        upstream = "\n".join(f"        server {host}:{port};" for host, port in endpoints)
        self.nginx_log_path_host = f"{self.plan.config.remote_dir('lb', self.ctx.sample_slug)}/nginx_access_timing.csv"
        nginx_conf = (
            "worker_processes auto;\n"
            "events {\n"
            "    worker_connections 4096;\n"
            "}\n"
            "http {\n"
            "    log_format baxbench_timing "
            "        '$msec,$status,$request_method,$uri,$request_time,$upstream_response_time,$upstream_connect_time,$upstream_header_time';\n"
            f"    access_log {self.nginx_log_path_container} baxbench_timing;\n"
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
        remote_lb_dir = self.plan.config.remote_dir("lb", self.ctx.sample_slug)
        remote_nginx_conf = f"{remote_lb_dir}/nginx.conf"
        remote_exec.ssh(self.plan.lb_host, f"mkdir -p {shlex.quote(remote_lb_dir)}", self.logger).check_returncode()
        write_cmd = f"cat > {shlex.quote(remote_nginx_conf)} <<'EOF'\n{nginx_conf}\nEOF\n"
        remote_exec.ssh(self.plan.lb_host, f"bash -lc {shlex.quote(write_cmd)}", self.logger)

        lbn_q = shlex.quote(self.ctx.lb_container_name)
        lb_res = self.system_topology.lb_resources
        pin_lb = lb_res.bash_apply_taskset_to_container(lbn_q)
        lb_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {lbn_q} >/dev/null 2>&1 || true; "
            f"docker run -d --name {lbn_q} "
            + " ".join(
                f"--label {shlex.quote(k + '=' + v)}"
                for k, v in {"baxbench.sample": self.ctx.sample_slug, "baxbench.role": "lb"}.items()
            )
            + " "
            f"{lb_res.docker_run_flags()} "
            f"-p 0.0.0.0:{self.plan.app_port}:80 "
            f"-v {shlex.quote(remote_nginx_conf)}:/etc/nginx/nginx.conf:ro "
            f"-v {shlex.quote(remote_lb_dir)}:{shlex.quote(remote_lb_dir)} "
            "nginx:1.27-alpine"
        )
        if pin_lb:
            lb_cmd += f"; {pin_lb}"

        # Always start fresh: remove any baxbench-managed LB container on the LB host.
        self.runtime.docker_rm_by_labels(
            self.plan.lb_host,
            labels={"baxbench.role": "lb"},
        )
        remote_exec.ssh(self.plan.lb_host, f'bash -lc "{lb_cmd}"', self.logger)

    def lb_target_for_loader(self, load_host: str) -> str:
        if not self.plan.lb_host:
            raise ValueError("LoadBalancerManager.lb_target_for_loader called but lb_host is empty")
        return self.ctx.lb_net_host

    def wait_ready(self) -> None:
        remote_exec.wait_for_remote_http(
            self.lb_target_for_loader(self.plan.load_host_master),
            self.plan.app_port,
            self.plan.config,
            self.env,
            self.logger,
        )

    def copy_timing_access_log(self) -> None:
        from bench_diagnostics.paths import distributed_host_dir

        lb_stats_dir = distributed_host_dir(
            self.ctx.sample_dir, host_slug(self.plan.lb_host)
        )
        materialize_cmd = (
            "set -euo pipefail; "
            f"docker cp {shlex.quote(self.ctx.lb_container_name)}:{shlex.quote(self.nginx_log_path_container)} "
            f"{shlex.quote(self.nginx_log_path_host)} >/dev/null 2>&1 || true"
        )
        remote_exec.ssh(self.plan.lb_host, f"bash -lc {shlex.quote(materialize_cmd)}", self.logger)
        remote_exec.scp_from_remote(
            self.plan.lb_host,
            self.nginx_log_path_host,
            (lb_stats_dir / "nginx_access_timing.csv"),
            self.logger,
        )

    def cleanup(self) -> None:
        try:
            self.runtime.docker_rm_by_labels(
                self.plan.lb_host,
                labels={"baxbench.role": "lb"},
            )
        except Exception as exc:
            self.logger.warning("Failed to cleanup LB container: %s", exc)

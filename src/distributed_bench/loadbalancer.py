from __future__ import annotations

import concurrent.futures
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

    def _ensure_lb_tunnels(self) -> tuple[str, list[tuple[str, int]]]:
        lb_host_ip = self.ctx.lb_net_host
        if lb_host_ip.startswith("127."):
            lb_host_ip = remote_exec.resolve_remote_primary_ipv4(self.plan.lb_host, self.logger)

        def _start_lb_tunnel(idx_h: tuple[int, str]) -> tuple[str, int, str]:
            _idx, host = idx_h
            local_lb_port = self.runtime.stable_port(17001, f"lb:{self.ctx.sample_slug}:{host}:{self.plan.lb_host}")
            tunnel_name = f"lb-{self.ctx.sample_slug}-{host_slug(host)}"
            tunnel_fn = remote_exec.ensure_remote_ssh_tunnel if self.toggles.keep_tunnels else remote_exec.start_remote_ssh_tunnel
            pidfile = tunnel_fn(
                host=self.plan.lb_host,
                tunnel_name=tunnel_name,
                local_port=local_lb_port,
                target_host="127.0.0.1",
                target_port=self.plan.app_port,
                ssh_dest=host,
                tunnel_dir=self.ctx.remote_tunnel_dir,
                logger=self.logger,
                bind_host="0.0.0.0",
            )
            return (host, local_lb_port, pidfile)

        endpoints: list[tuple[str, int]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(self.plan.backend_hosts) or 1)) as ex:
            for _h, local_lb_port, pidfile in ex.map(_start_lb_tunnel, list(enumerate(self.plan.backend_hosts))):
                self.ctx.active_tunnels.append((self.plan.lb_host, pidfile))
                self.ctx.active_lb_tunnels.append((self.plan.lb_host, pidfile))
                endpoints.append((lb_host_ip, local_lb_port))
        return lb_host_ip, endpoints

    def setup_or_reuse(self) -> None:
        _lb_host_ip, endpoints = self._ensure_lb_tunnels()
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

        lb_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {shlex.quote(self.ctx.lb_container_name)} >/dev/null 2>&1 || true; "
            f"docker run -d --name {shlex.quote(self.ctx.lb_container_name)} "
            + " ".join(
                f"--label {shlex.quote(k + '=' + v)}"
                for k, v in {"baxbench.sample": self.ctx.sample_slug, "baxbench.role": "lb"}.items()
            )
            + " "
            f"{self.system_topology.lb_resources.docker_run_flags()} "
            f"-p 0.0.0.0:{self.plan.app_port}:80 "
            f"-v {shlex.quote(remote_nginx_conf)}:/etc/nginx/nginx.conf:ro "
            f"-v {shlex.quote(remote_lb_dir)}:{shlex.quote(remote_lb_dir)} "
            "nginx:1.27-alpine"
        )

        if self.toggles.keep_lb:
            existing_lb = self.runtime.docker_ps_id(
                self.plan.lb_host,
                labels={"baxbench.sample": self.ctx.sample_slug, "baxbench.role": "lb"},
            )
            if existing_lb:
                self.logger.info("Reusing load balancer container on %s (BAXBENCH_KEEP_LB=1)", self.plan.lb_host)
                reload_cmd = (
                    "set -euo pipefail; "
                    f"docker exec {shlex.quote(self.ctx.lb_container_name)} nginx -s reload >/dev/null 2>&1 "
                    f"|| docker restart {shlex.quote(self.ctx.lb_container_name)} >/dev/null"
                )
                remote_exec.ssh(self.plan.lb_host, f"bash -lc {shlex.quote(reload_cmd)}", self.logger)
            else:
                remote_exec.ssh(self.plan.lb_host, f'bash -lc "{lb_cmd}"', self.logger)
            return

        self.runtime.docker_rm_by_labels(
            self.plan.lb_host,
            labels={"baxbench.sample": self.ctx.sample_slug, "baxbench.role": "lb"},
        )
        remote_exec.ssh(self.plan.lb_host, f'bash -lc "{lb_cmd}"', self.logger)

    def lb_target_for_loader(self) -> str:
        return "127.0.0.1" if self.plan.load_host == self.plan.lb_host else self.ctx.lb_net_host

    def wait_ready(self) -> None:
        remote_exec.wait_for_remote_http(
            self.lb_target_for_loader(),
            self.plan.app_port,
            self.plan.config,
            self.env,
            self.logger,
        )

    def copy_timing_access_log(self) -> None:
        lb_stats_dir = self.ctx.sample_dir / "stats" / host_slug(self.plan.lb_host)
        lb_stats_dir.mkdir(parents=True, exist_ok=True)
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
        if self.toggles.keep_lb:
            self.logger.info(
                "Keeping load balancer container %s on %s (BAXBENCH_KEEP_LB=1)",
                self.ctx.lb_container_name,
                self.plan.lb_host,
            )
            return
        try:
            remote_exec.ssh(
                self.plan.lb_host,
                f"bash -lc \"docker rm -f {shlex.quote(self.ctx.lb_container_name)} >/dev/null 2>&1 || true\"",
                self.logger,
            )
        except Exception as exc:
            self.logger.warning("Failed to cleanup LB container: %s", exc)

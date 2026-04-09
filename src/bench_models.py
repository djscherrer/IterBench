"""
Shared data models for remote + distributed benchmarking.

This module intentionally contains *no* SSH/SCP/subprocess side effects.
It is safe to import from task orchestration / CLI.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from dataclasses import dataclass

from env.base import Env


def host_slug(host: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in host)


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


@dataclass(frozen=True)
class DistributedBenchPlan:
    config: RemoteConfig
    env: Env
    needs_db: bool

    bench_users: int
    bench_spawn_rate: int
    bench_run_time_s: int

    app_port: int

    backend_hosts: tuple[str, ...]
    load_host: str
    lb_host: str
    db_host: str

    def __post_init__(self) -> None:
        if not self.backend_hosts:
            raise ValueError("No backend hosts selected for remote benchmarking")
        bad = [
            h
            for h in ([*self.backend_hosts, self.lb_host] + ([self.db_host] if self.needs_db else []))
            if "@" in h
        ]
        if bad:
            raise ValueError(
                "Remote multi-host bench requires network-reachable hostnames without 'user@'. "
                f"Got: {bad}. "
                "Recommendation: add host aliases in ~/.ssh/config and use those aliases here."
            )

    @property
    def locust_run_time(self) -> str:
        return f"{int(self.bench_run_time_s)}s"

    @classmethod
    def from_args(
        cls,
        *,
        config: RemoteConfig,
        env: Env,
        needs_db: bool,
        bench_users: int | None,
        bench_spawn_rate: int | None,
        bench_run_time: int | None,
    ) -> "DistributedBenchPlan":
        backend_hosts = tuple(config.selected_backend_hosts())
        load_host = config.load_host
        lb_host = config.selected_lb_host()
        db_host = config.selected_db_host() if needs_db else ""

        bu = int(bench_users) if bench_users is not None else 7200
        bsr = int(bench_spawn_rate) if bench_spawn_rate is not None else 40
        brt = int(bench_run_time) if bench_run_time is not None else 180

        app_port = config.app_port or env.port

        return cls(
            config=config,
            env=env,
            needs_db=needs_db,
            bench_users=bu,
            bench_spawn_rate=bsr,
            bench_run_time_s=brt,
            app_port=app_port,
            backend_hosts=backend_hosts,
            load_host=load_host,
            lb_host=lb_host,
            db_host=db_host,
        )


@dataclass
class DistributedBenchContext:
    plan: DistributedBenchPlan
    sample_slug: str
    sample_dir: pathlib.Path
    image_cache_dir: pathlib.Path | None
    image_id: str
    locustfile: pathlib.Path
    csv_prefix: pathlib.Path
    timeout: int
    logger: logging.Logger

    container_name: str
    lb_container_name: str
    db_container_name: str

    remote_app_dirs: dict[str, str]
    remote_load_dir: str
    remote_env_dir: str
    remote_tunnel_dir: str

    tar_path: pathlib.Path
    remote_tars: dict[str, str]

    backend_net_hosts: dict[str, str]
    lb_net_host: str
    db_net_host: str

    involved_hosts: list[str]

    active_tunnels: list[tuple[str, str]]
    active_lb_tunnels: list[tuple[str, str]]
    backend_container_names: dict[str, str]
    db_host_for_backend: dict[str, str]
    db_port_for_backend: dict[str, int]

    @property
    def env_vars_base(self) -> str:
        return f"-e PORT={self.plan.env.port} "

    @classmethod
    def create(
        cls,
        *,
        plan: DistributedBenchPlan,
        sample_slug: str,
        sample_dir: pathlib.Path,
        image_cache_dir: pathlib.Path | None,
        image_id: str,
        locustfile: pathlib.Path,
        csv_prefix: pathlib.Path,
        timeout: int,
        logger: logging.Logger,
    ) -> "DistributedBenchContext":
        # Import here to avoid cyclic imports (remote_exec imports RemoteConfig type).
        import remote_exec

        remote_app_dirs: dict[str, str] = {
            h: plan.config.remote_dir("app", sample_slug, host_slug(h)) for h in plan.backend_hosts
        }
        remote_load_dir = plan.config.remote_dir("load", sample_slug)
        remote_env_dir = plan.config.remote_dir("load", ".venv")

        container_name = f"baxbench-{sample_slug}-{uuid.uuid4().hex[:8]}"
        db_container_name = container_name + "-db"
        lb_container_name = f"baxbench-{sample_slug}-lb"

        tar_root = image_cache_dir if image_cache_dir is not None else sample_dir
        tar_path = remote_exec.save_image_tar(image_id, tar_root, logger)
        remote_tars: dict[str, str] = {h: f"{remote_app_dirs[h]}/{tar_path.name}" for h in plan.backend_hosts}

        backend_net_hosts: dict[str, str] = {h: remote_exec.resolve_ipv4(h) for h in plan.backend_hosts}
        lb_net_host = remote_exec.resolve_ipv4(plan.lb_host)
        db_net_host = remote_exec.resolve_ipv4(plan.db_host) if plan.needs_db else ""

        remote_tunnel_dir = plan.config.remote_dir("tunnels", sample_slug)
        involved_hosts = sorted(
            set([*plan.backend_hosts, plan.lb_host] + ([plan.db_host] if plan.needs_db else []))
        )

        backend_container_names: dict[str, str] = {
            h: f"{container_name}-app-{host_slug(h)}" for h in plan.backend_hosts
        }

        return cls(
            plan=plan,
            sample_slug=sample_slug,
            sample_dir=sample_dir,
            image_cache_dir=image_cache_dir,
            image_id=image_id,
            locustfile=locustfile,
            csv_prefix=csv_prefix,
            timeout=timeout,
            logger=logger,
            container_name=container_name,
            lb_container_name=lb_container_name,
            db_container_name=db_container_name,
            remote_app_dirs=remote_app_dirs,
            remote_load_dir=remote_load_dir,
            remote_env_dir=remote_env_dir,
            remote_tunnel_dir=remote_tunnel_dir,
            tar_path=tar_path,
            remote_tars=remote_tars,
            backend_net_hosts=backend_net_hosts,
            lb_net_host=lb_net_host,
            db_net_host=db_net_host,
            involved_hosts=involved_hosts,
            active_tunnels=[],
            active_lb_tunnels=[],
            backend_container_names=backend_container_names,
            db_host_for_backend={},
            db_port_for_backend={},
        )


__all__ = [
    "RemoteConfig",
    "DistributedBenchPlan",
    "DistributedBenchContext",
    "host_slug",
]


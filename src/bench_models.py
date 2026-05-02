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
    backend_hosts: tuple[str, ...]
    remote_base_dir: str
    
    # Explicit Locust topology:
    # - load_master: the host that runs the Locust master process (single hostname)
    # - load_workers: hosts that run Locust worker processes (may include the same host as load_master)
    load_master: str
    load_workers: tuple[str, ...] = ()

    lb_host: str | None = None
    db_hosts: tuple[str, ...] = ()
    app_private_addr: str | None = None
    app_port: int | None = None
    max_startup_wait: float | None = None
    poll_interval: float = 0.5
    request_timeout: float = 2.0

    def __post_init__(self) -> None:
        self.backend_hosts = tuple(str(h).strip() for h in self.backend_hosts if str(h).strip())
        self.load_master = str(self.load_master).strip()
        self.load_workers = tuple(str(h).strip() for h in self.load_workers if str(h).strip())
        self.db_hosts = tuple(str(h).strip() for h in self.db_hosts if str(h).strip())
        if self.lb_host is not None and not str(self.lb_host).strip():
            self.lb_host = ""

        if not self.backend_hosts:
            raise ValueError("Remote bench requires at least one backend host")
        if not self.load_master:
            raise ValueError("Remote bench requires load_master")

    @property
    def backend_host_master(self) -> str:
        return self.backend_hosts[0]

    @property
    def load_host_master(self) -> str:
        return self.load_master

    @property
    def db_host_master(self) -> str:
        if not self.db_hosts:
            raise ValueError("No db_hosts configured")
        return self.db_hosts[0]

    def effective_lb_host(self) -> str:
        # lb_host semantics:
        # - None: legacy default, assume LB runs on the master load host
        # - "": explicitly no LB
        # - "<host>": dedicated LB host
        if self.lb_host is None:
            return self.load_host_master
        return str(self.lb_host)

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
    # Explicit Locust topology.
    load_master: str
    load_workers: tuple[str, ...]
    lb_host: str
    db_hosts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.backend_hosts:
            raise ValueError("No backend hosts selected for remote benchmarking")
        if not self.load_master:
            raise ValueError("No load master selected for remote benchmarking")
        if self.needs_db and not self.db_hosts:
            raise ValueError("DB is required but no db_hosts were selected for remote benchmarking")
        if self.needs_db and len(self.db_hosts) != 1:
            raise ValueError(
                "Currently only a single DB is supported for distributed benchmarking. "
                f"Got db_hosts={self.db_hosts}"
            )
        bad = [
            h
            for h in (
                [*self.backend_hosts, self.lb_host, self.load_master, *self.load_workers]
                + (list(self.db_hosts) if self.needs_db else [])
            )
            if "@" in h
        ]
        if bad:
            raise ValueError(
                "Remote multi-host bench requires network-reachable hostnames without 'user@'. "
                f"Got: {bad}. "
                "Recommendation: add host aliases in ~/.ssh/config and use those aliases here."
            )

        # If there is no dedicated load balancer, load must go directly to exactly one backend.
        # (Multi-backend without LB is ambiguous and would require client-side sharding.)
        if (not self.lb_host) and len(self.backend_hosts) != 1:
            raise ValueError(
                "No load balancer configured (lb_host is empty), but multiple backends were selected. "
                "Either configure lb_host or select exactly one backend host."
            )

    @property
    def db_host(self) -> str:
        return self.db_hosts[0] if self.db_hosts else ""

    @property
    def load_host_master(self) -> str:
        return self.load_master

    @property
    def load_all_hosts(self) -> tuple[str, ...]:
        ordered = [self.load_master, *self.load_workers]
        out: list[str] = []
        seen: set[str] = set()
        for h in ordered:
            if h and h not in seen:
                out.append(h)
                seen.add(h)
        return tuple(out)

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
        backend_hosts = tuple(config.backend_hosts)
        load_master = str(config.load_master).strip()
        load_workers = tuple(config.load_workers)
        lb_host = config.effective_lb_host()
        db_hosts = tuple(config.db_hosts) if needs_db else ()

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
            load_master=load_master,
            load_workers=load_workers,
            lb_host=lb_host,
            db_hosts=db_hosts,
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

    tar_path: pathlib.Path
    remote_tars: dict[str, str]

    backend_net_hosts: dict[str, str]
    lb_net_host: str
    db_net_host: str

    involved_hosts: list[str]
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
        # DB container name is stable so log collection and `docker exec` calls remain predictable.
        db_container_name = f"baxbench-{sample_slug}-db"
        lb_container_name = f"baxbench-{sample_slug}-lb"

        tar_root = image_cache_dir if image_cache_dir is not None else sample_dir
        tar_path = remote_exec.save_image_tar(image_id, tar_root, logger)
        remote_tars: dict[str, str] = {h: f"{remote_app_dirs[h]}/{tar_path.name}" for h in plan.backend_hosts}

        def _resolve_net_ip(host: str) -> str:
            # Prefer the 10.233.* namespace in this cluster setup.
            try:
                return remote_exec.resolve_remote_preferred_ipv4(host, logger, preferred_prefixes=("10.233.",))
            except Exception:
                logger.warning("Failed to resolve preferred net IP for %s; falling back to remote primary", host)
                return remote_exec.resolve_remote_primary_ipv4(host, logger)

        resolved_backend_net_hosts = {h: _resolve_net_ip(h) for h in plan.backend_hosts}
        resolved_lb_net_host = _resolve_net_ip(plan.lb_host) if plan.lb_host else ""
        resolved_db_net_host = _resolve_net_ip(plan.db_host) if plan.needs_db else ""
        involved_hosts = sorted(
            set(
                [*plan.backend_hosts]
                + list(plan.load_all_hosts)
                + ([plan.lb_host] if plan.lb_host else [])
                + ([plan.db_host] if plan.needs_db else [])
            )
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
            tar_path=tar_path,
            remote_tars=remote_tars,
            backend_net_hosts=resolved_backend_net_hosts,
            lb_net_host=resolved_lb_net_host,
            db_net_host=resolved_db_net_host,
            involved_hosts=involved_hosts,
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


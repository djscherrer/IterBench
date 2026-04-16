from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from bench_models import RemoteConfig


@dataclass(frozen=True)
class ContainerResources:
    cpus: float | None = None
    cpuset_cpus: str | None = None
    memory: str | None = None
    memory_swap: str | None = None
    pids_limit: int | None = None
    extra_run_args: tuple[str, ...] = ()

    def docker_run_flags(self) -> str:
        flags: list[str] = []
        if self.cpus is not None:
            flags.append(f"--cpus={self.cpus}")
        if self.cpuset_cpus:
            flags.append(f"--cpuset-cpus={shlex.quote(self.cpuset_cpus)}")
        if self.memory:
            flags.append(f"--memory={shlex.quote(self.memory)}")
        if self.memory_swap:
            flags.append(f"--memory-swap={shlex.quote(self.memory_swap)}")
        if self.pids_limit is not None:
            flags.append(f"--pids-limit={self.pids_limit}")
        flags.extend(self.extra_run_args)
        return " ".join(flags)


@dataclass(frozen=True)
class SystemTopology:
    name: str
    backend_hosts: tuple[str, ...] | None = None
    # A single DB host (str) or multiple DB hosts (tuple[str, ...]).
    # The current distributed runner supports exactly one DB, but we store this
    # as a tuple for future extensibility.
    db_hosts: str | tuple[str, ...] | None = None
    lb_host: str | None = None
    # Load generator hosts (tuple-of-one allowed).
    load_hosts: tuple[str, ...] | None = None
    app_port: int | None = None
    backend_resources: ContainerResources = field(default_factory=ContainerResources)
    db_resources: ContainerResources = field(default_factory=ContainerResources)
    lb_resources: ContainerResources = field(default_factory=ContainerResources)
    load_taskset_cpus: str | None = None

    def has_host_mapping(self) -> bool:
        return bool(self.backend_hosts) and bool(self.load_hosts)

    def to_remote_config(
        self,
        *,
        remote_base_dir: str,
        app_private_addr: str | None = None,
        app_port: int | None = None,
    ) -> RemoteConfig:
        if not self.has_host_mapping():
            raise ValueError(
                f"System topology '{self.name}' does not define backend_hosts/load_hosts. "
                "Add those fields in system_configs/registry.py or pass --bench-app-hosts/--bench-loader-host."
            )
        backend_hosts = tuple(self.backend_hosts or ())
        load_hosts = tuple(self.load_hosts or ())
        if not load_hosts:
            raise ValueError(
                f"System topology '{self.name}' does not define load_hosts. "
                "Add load_hosts in system_configs/registry.py or pass --bench-loader-host."
            )
        raw_db = self.db_hosts
        db_hosts = tuple(raw_db) if isinstance(raw_db, tuple) else ((raw_db,) if raw_db else ())
        db_hosts = tuple(str(h).strip() for h in db_hosts if h is not None and str(h).strip())
        return RemoteConfig(
            backend_hosts=backend_hosts,
            app_private_addr=app_private_addr,
            load_hosts=load_hosts,
            remote_base_dir=remote_base_dir,
            app_port=app_port if app_port is not None else self.app_port,
            lb_host=self.lb_host,
            db_hosts=db_hosts,
        )

    def apply_to_remote_config(self, base: RemoteConfig) -> RemoteConfig:
        backend_hosts = tuple(self.backend_hosts) if self.backend_hosts is not None else base.backend_hosts
        backend_host_master = backend_hosts[0] if backend_hosts else base.backend_host_master
        raw_db = self.db_hosts
        db_hosts = tuple(raw_db) if isinstance(raw_db, tuple) else ((raw_db,) if raw_db else ())
        db_hosts = tuple(str(h).strip() for h in db_hosts if h is not None and str(h).strip())
        load_hosts = tuple(self.load_hosts) if self.load_hosts is not None else base.load_hosts
        return RemoteConfig(
            backend_hosts=backend_hosts,
            app_private_addr=base.app_private_addr,
            load_hosts=load_hosts,
            remote_base_dir=base.remote_base_dir,
            app_port=self.app_port if self.app_port is not None else base.app_port,
            lb_host=self.lb_host if self.lb_host is not None else base.lb_host,
            db_hosts=db_hosts if db_hosts else base.db_hosts,
            max_startup_wait=base.max_startup_wait,
            poll_interval=base.poll_interval,
            request_timeout=base.request_timeout,
        )

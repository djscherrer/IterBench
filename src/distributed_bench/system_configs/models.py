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
    db_host: str | None = None
    lb_host: str | None = None
    load_host: str | None = None
    app_port: int | None = None
    backend_resources: ContainerResources = field(default_factory=ContainerResources)
    db_resources: ContainerResources = field(default_factory=ContainerResources)
    lb_resources: ContainerResources = field(default_factory=ContainerResources)
    load_taskset_cpus: str | None = None

    def has_host_mapping(self) -> bool:
        return bool(self.backend_hosts) and bool(self.load_host)

    def to_remote_config(
        self,
        *,
        remote_base_dir: str,
        app_private_addr: str | None = None,
        app_port: int | None = None,
    ) -> RemoteConfig:
        if not self.has_host_mapping():
            raise ValueError(
                f"System topology '{self.name}' does not define backend_hosts/load_host. "
                "Add those fields in system_configs/registry.py or pass --bench-app-hosts/--bench-loader-host."
            )
        backend_hosts = list(self.backend_hosts or ())
        return RemoteConfig(
            app_host=backend_hosts[0],
            app_hosts=backend_hosts,
            app_private_addr=app_private_addr,
            load_host=str(self.load_host),
            remote_base_dir=remote_base_dir,
            app_port=app_port if app_port is not None else self.app_port,
            lb_host=self.lb_host,
            db_host=self.db_host,
        )

    def apply_to_remote_config(self, base: RemoteConfig) -> RemoteConfig:
        app_hosts = list(self.backend_hosts) if self.backend_hosts is not None else base.app_hosts
        app_host = app_hosts[0] if app_hosts else base.app_host
        return RemoteConfig(
            app_host=app_host,
            app_hosts=app_hosts,
            app_private_addr=base.app_private_addr,
            load_host=self.load_host or base.load_host,
            remote_base_dir=base.remote_base_dir,
            app_port=self.app_port if self.app_port is not None else base.app_port,
            lb_host=self.lb_host if self.lb_host is not None else base.lb_host,
            db_host=self.db_host if self.db_host is not None else base.db_host,
            max_startup_wait=base.max_startup_wait,
            poll_interval=base.poll_interval,
            request_timeout=base.request_timeout,
        )

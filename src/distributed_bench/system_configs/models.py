from __future__ import annotations

import shlex
from dataclasses import dataclass, field, replace

from bench_models import RemoteConfig


@dataclass(frozen=True)
class ContainerResourcesDocker:
    """Docker ``docker run`` resource flags (cgroup limits where the daemon allows them)."""

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
class ContainerResources:
    """
    Per-role resources: optional ``taskset`` CPU list (host PIDs) and Docker memory,
    plus nested Docker-only limits in ``docker``.
    """

    taskset_cpus: str | None = None
    memory: str | None = None
    docker: ContainerResourcesDocker = field(default_factory=ContainerResourcesDocker)

    def docker_run_flags(self) -> str:
        merged_mem = self.memory if self.memory is not None else self.docker.memory
        return replace(self.docker, memory=merged_mem).docker_run_flags()

    def bash_apply_taskset_to_container(self, container_name_word: str) -> str:
        """
        Return a bash fragment that pins the container's root PID (and threads) with
        ``taskset``, or the empty string if ``taskset_cpus`` is unset.

        ``container_name_word`` must already be safe for embedding in ``bash -lc`` double
        quotes (typically ``shlex.quote(container_name)``).
        """
        if not self.taskset_cpus:
            return ""
        cpus_arg = shlex.quote(self.taskset_cpus)
        # No double quotes in this fragment: backend/lb/db wrap the whole script in
        # ``bash -lc "..."`` and nested quotes would break that.
        return (
            f"root_pid=$(docker inspect -f '{{{{.State.Pid}}}}' {container_name_word}); "
            f"if [[ -n $root_pid && $root_pid != 0 ]]; then "
            f"taskset -a -c {cpus_arg} -p $root_pid >/dev/null 2>&1 || true; fi"
        )


@dataclass(frozen=True)
class SystemTopology:
    name: str
    backend_hosts: tuple[str, ...] | None = None
    # A single DB host (str) or multiple DB hosts (tuple[str, ...]).
    # The current distributed runner supports exactly one DB, but we store this
    # as a tuple for future extensibility.
    db_hosts: str | tuple[str, ...] | None = None
    lb_host: str | None = None
    # Locust topology:
    # - load_master: the host that runs the Locust master process (single hostname)
    # - load_workers: hosts that run Locust worker processes (may include the same host as load_master)
    load_master: str | None = None
    load_workers: tuple[str, ...] | None = None
    app_port: int | None = None
    backend_resources: ContainerResources = field(default_factory=ContainerResources)
    db_resources: ContainerResources = field(default_factory=ContainerResources)
    lb_resources: ContainerResources = field(default_factory=ContainerResources)
    load_resources: ContainerResources = field(default_factory=ContainerResources)

    def has_host_mapping(self) -> bool:
        if not self.backend_hosts:
            return False
        return bool(self.load_master and str(self.load_master).strip())

    def to_remote_config(
        self,
        *,
        remote_base_dir: str,
        app_private_addr: str | None = None,
        app_port: int | None = None,
    ) -> RemoteConfig:
        if not self.has_host_mapping():
            raise ValueError(
                f"System topology '{self.name}' does not define backend_hosts/load_master. "
                "Add those fields in system_configs/registry.py or pass --bench-app-hosts/--bench-load-master."
            )
        backend_hosts = tuple(self.backend_hosts or ())
        load_master = str(self.load_master or "").strip()
        if not load_master:
            raise ValueError(f"System topology '{self.name}' does not define load_master.")
        load_workers = tuple(self.load_workers or ())
        raw_db = self.db_hosts
        db_hosts = tuple(raw_db) if isinstance(raw_db, tuple) else ((raw_db,) if raw_db else ())
        db_hosts = tuple(str(h).strip() for h in db_hosts if h is not None and str(h).strip())
        return RemoteConfig(
            backend_hosts=backend_hosts,
            app_private_addr=app_private_addr,
            remote_base_dir=remote_base_dir,
            app_port=app_port if app_port is not None else self.app_port,
            lb_host=self.lb_host,
            db_hosts=db_hosts,
            load_master=load_master,
            load_workers=load_workers,
        )

    def apply_to_remote_config(self, base: RemoteConfig) -> RemoteConfig:
        backend_hosts = tuple(self.backend_hosts) if self.backend_hosts is not None else base.backend_hosts
        raw_db = self.db_hosts
        db_hosts = tuple(raw_db) if isinstance(raw_db, tuple) else ((raw_db,) if raw_db else ())
        db_hosts = tuple(str(h).strip() for h in db_hosts if h is not None and str(h).strip())
        load_master = str(self.load_master).strip() if self.load_master is not None else base.load_master
        load_workers = tuple(self.load_workers) if self.load_workers is not None else base.load_workers
        return RemoteConfig(
            backend_hosts=backend_hosts,
            app_private_addr=base.app_private_addr,
            remote_base_dir=base.remote_base_dir,
            app_port=self.app_port if self.app_port is not None else base.app_port,
            lb_host=self.lb_host if self.lb_host is not None else base.lb_host,
            db_hosts=db_hosts if db_hosts else base.db_hosts,
            max_startup_wait=base.max_startup_wait,
            poll_interval=base.poll_interval,
            request_timeout=base.request_timeout,
            load_master=load_master,
            load_workers=load_workers,
        )

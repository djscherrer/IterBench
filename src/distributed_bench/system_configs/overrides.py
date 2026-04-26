from __future__ import annotations

import os
from dataclasses import replace

from .models import ContainerResources, ContainerResourcesDocker, SystemTopology


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    txt = raw.strip()
    if not txt:
        return None
    return float(txt)


def _env_str(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    txt = raw.strip()
    return txt or None


def _apply_container_resources_docker_overrides(
    base: ContainerResourcesDocker, *, role_prefix: str
) -> ContainerResourcesDocker:
    cpus = _env_float(f"BAXBENCH_{role_prefix}_CPUS")
    cpuset_cpus = _env_str(f"BAXBENCH_{role_prefix}_CPUSET_DOCKER")
    pids_limit_raw = _env_str(f"BAXBENCH_{role_prefix}_PIDS_LIMIT")
    pids_limit = int(pids_limit_raw) if pids_limit_raw else None
    memory_swap = _env_str(f"BAXBENCH_{role_prefix}_MEMORY_SWAP")
    return replace(
        base,
        cpus=cpus if cpus is not None else base.cpus,
        cpuset_cpus=cpuset_cpus if cpuset_cpus is not None else base.cpuset_cpus,
        pids_limit=pids_limit if pids_limit is not None else base.pids_limit,
        memory_swap=memory_swap if memory_swap is not None else base.memory_swap,
    )


def _apply_container_resources_overrides(base: ContainerResources, *, role_prefix: str) -> ContainerResources:
    # Host-side affinity (``taskset`` on the container root PID). Historically this used
    # ``BAXBENCH_*_CPUSET`` when that flag was passed to Docker; rootless setups often
    # cannot set cgroup CPU affinity, so the same env name now maps here.
    taskset = _env_str(f"BAXBENCH_{role_prefix}_TASKSET_CPUS")
    if taskset is None:
        taskset = _env_str(f"BAXBENCH_{role_prefix}_CPUSET")
    memory = _env_str(f"BAXBENCH_{role_prefix}_MEMORY")
    docker = _apply_container_resources_docker_overrides(base.docker, role_prefix=role_prefix)
    return replace(
        base,
        taskset_cpus=taskset if taskset is not None else base.taskset_cpus,
        memory=memory if memory is not None else base.memory,
        docker=docker,
    )


def apply_system_topology_env_overrides(topology: SystemTopology) -> SystemTopology:
    load_taskset = _env_str("BAXBENCH_LOAD_TASKSET_CPUS")
    if load_taskset is None:
        load_taskset = _env_str("BAXBENCH_LOAD_CPUSET")
    load_res = replace(
        topology.load_resources,
        taskset_cpus=load_taskset if load_taskset is not None else topology.load_resources.taskset_cpus,
    )
    return replace(
        topology,
        backend_resources=_apply_container_resources_overrides(
            topology.backend_resources,
            role_prefix="BACKEND",
        ),
        db_resources=_apply_container_resources_overrides(
            topology.db_resources,
            role_prefix="DB",
        ),
        lb_resources=_apply_container_resources_overrides(
            topology.lb_resources,
            role_prefix="LB",
        ),
        load_resources=load_res,
    )

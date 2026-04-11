from __future__ import annotations

import os
from dataclasses import replace

from .models import ContainerResources, SystemTopology


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


def _apply_container_resource_overrides(base: ContainerResources, *, role_prefix: str) -> ContainerResources:
    cpus = _env_float(f"BAXBENCH_{role_prefix}_CPUS")
    memory = _env_str(f"BAXBENCH_{role_prefix}_MEMORY")
    cpuset_cpus = _env_str(f"BAXBENCH_{role_prefix}_CPUSET")
    pids_limit_raw = _env_str(f"BAXBENCH_{role_prefix}_PIDS_LIMIT")
    pids_limit = int(pids_limit_raw) if pids_limit_raw else None
    memory_swap = _env_str(f"BAXBENCH_{role_prefix}_MEMORY_SWAP")
    return replace(
        base,
        cpus=cpus if cpus is not None else base.cpus,
        memory=memory if memory is not None else base.memory,
        cpuset_cpus=cpuset_cpus if cpuset_cpus is not None else base.cpuset_cpus,
        pids_limit=pids_limit if pids_limit is not None else base.pids_limit,
        memory_swap=memory_swap if memory_swap is not None else base.memory_swap,
    )


def apply_system_topology_env_overrides(topology: SystemTopology) -> SystemTopology:
    load_taskset = _env_str("BAXBENCH_LOAD_TASKSET_CPUS")
    return replace(
        topology,
        backend_resources=_apply_container_resource_overrides(
            topology.backend_resources,
            role_prefix="BACKEND",
        ),
        db_resources=_apply_container_resource_overrides(
            topology.db_resources,
            role_prefix="DB",
        ),
        lb_resources=_apply_container_resource_overrides(
            topology.lb_resources,
            role_prefix="LB",
        ),
        load_taskset_cpus=load_taskset if load_taskset is not None else topology.load_taskset_cpus,
    )

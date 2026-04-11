from __future__ import annotations

from .models import ContainerResources, SystemTopology


SYSTEM_TOPOLOGY_REGISTRY: dict[str, SystemTopology] = {
    # Default topology for the current remote lab.
    "default": SystemTopology(
        name="default",
        backend_hosts=("r630-02", "r630-03", "r630-04"),
        load_host="r630-08",
        lb_host="r630-08",
        db_host="r630-05",
    ),
    # Example preset that caps backend resources.
    "balanced-2-backend": SystemTopology(
        name="balanced-2-backend",
        backend_hosts=("r630-02", "r630-03"),
        load_host="r630-08",
        lb_host="r630-08",
        db_host="r630-05",
        backend_resources=ContainerResources(cpus=2.0, memory="3g"),
        db_resources=ContainerResources(cpus=2.0, memory="4g"),
        lb_resources=ContainerResources(cpus=1.0, memory="1g"),
    ),
    # Example preset for stronger backend limits.
    "high-throughput": SystemTopology(
        name="high-throughput",
        backend_hosts=("r630-02", "r630-03", "r630-04"),
        load_host="r630-08",
        lb_host="r630-08",
        db_host="r630-05",
        backend_resources=ContainerResources(cpus=4.0, memory="6g"),
        db_resources=ContainerResources(cpus=4.0, memory="8g"),
        lb_resources=ContainerResources(cpus=2.0, memory="2g"),
    ),
}


def resolve_system_topology(name: str | None) -> SystemTopology:
    key = (name or "default").strip()
    if key not in SYSTEM_TOPOLOGY_REGISTRY:
        known = ", ".join(sorted(SYSTEM_TOPOLOGY_REGISTRY))
        raise ValueError(f"Unknown system topology '{key}'. Known topologies: {known}")
    return SYSTEM_TOPOLOGY_REGISTRY[key]

from __future__ import annotations

from .models import ContainerResources, SystemTopology


SYSTEM_TOPOLOGY_REGISTRY: dict[str, SystemTopology] = {
    # Default topology for the current remote lab.

    "2C-1B-1DB": SystemTopology(
        name="2C-1B-1DB",
        backend_hosts=("r630-03",),
        load_master="r630-08",
        load_workers=("r630-08", "r630-02",),
        lb_host="",
        db_hosts=("r630-04",),
    ),

    "2C-2B-1DB": SystemTopology(
        name="2C-2B-1DB",
        backend_hosts=("r630-03", "r630-04"),
        load_master="r630-08",
        load_workers=("r630-08", "r630-02",),
        lb_host="r630-08",
        db_hosts=("r630-05",),
    ),
}


def resolve_system_topology(name: str | None) -> SystemTopology:
    key = (name or "default").strip()
    if key not in SYSTEM_TOPOLOGY_REGISTRY:
        known = ", ".join(sorted(SYSTEM_TOPOLOGY_REGISTRY))
        raise ValueError(f"Unknown system topology '{key}'. Known topologies: {known}")
    return SYSTEM_TOPOLOGY_REGISTRY[key]

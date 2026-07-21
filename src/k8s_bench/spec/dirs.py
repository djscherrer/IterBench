"""Create and initialize per-sample iteration directories under ``iterations/``."""

from __future__ import annotations

from pathlib import Path

from workspace import (
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    iteration_dir,
    iteration_spec_path,
    new_iteration_id,
    normalize_iteration_id,
)
from .models import K8sWorkloadSpec


def prepare_iteration(
    sample_dir: Path,
    iteration_id: str | None,
    *,
    spec: K8sWorkloadSpec | None = None,
    write_spec: bool = True,
) -> Path:
    iid = normalize_iteration_id(iteration_id or new_iteration_id(sample_dir))
    iteration_path = iteration_dir(sample_dir, iid)
    ensure_iteration_core_layout(iteration_path)
    spec_path = iteration_spec_path(iteration_path)
    existing = find_iteration_spec_path(iteration_path)
    if spec is not None:
        spec_to_write = spec
    elif existing is not None:
        spec_to_write = K8sWorkloadSpec.from_yaml_file(existing)
    else:
        raise FileNotFoundError(
            f"No spec at {spec_path}; pass iteration_id with existing spec or provide spec=."
        )
    if write_spec:
        resolved = K8sWorkloadSpec.from_mapping(
            spec_to_write.to_yaml_dict(),
            iteration_id=iid,
        )
        resolved.write_yaml(spec_path)
    return iteration_path

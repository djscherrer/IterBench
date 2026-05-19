from __future__ import annotations

import logging
from pathlib import Path

from .deploy import deploy_iteration
from .models import K8sWorkloadSpec
from .paths import iteration_spec_path, new_iteration_id, normalize_iteration_id
from .render import render_iteration


def prepare_iteration(
    sample_dir: Path,
    iteration_id: str | None,
    *,
    spec: K8sWorkloadSpec | None = None,
    write_spec: bool = True,
) -> Path:
    """
    Create ``sample_dir/k8s_configs/<iteration>/`` and optionally write ``spec.yaml``.
    """
    iid = normalize_iteration_id(iteration_id or new_iteration_id(sample_dir))
    iteration_path = sample_dir / "k8s_configs" / iid
    iteration_path.mkdir(parents=True, exist_ok=True)
    spec_path = iteration_spec_path(iteration_path)
    if spec is not None:
        spec_to_write = spec
    elif not spec_path.exists():
        raise FileNotFoundError(
            f"No spec at {spec_path}; pass iteration_id with existing spec or provide spec=."
        )
    else:
        spec_to_write = K8sWorkloadSpec.from_yaml_file(spec_path)
    if write_spec:
        resolved = K8sWorkloadSpec.from_mapping(
            spec_to_write.to_yaml_dict(),
            iteration_id=iid,
        )
        resolved.write_yaml(spec_path)
    return iteration_path


def render_and_deploy(
    iteration_path: Path,
    *,
    wait_timeout_s: int = 300,
    logger: logging.Logger | None = None,
) -> None:
    render_iteration(iteration_path)
    result = deploy_iteration(iteration_path, wait_timeout_s=wait_timeout_s, logger=logger)
    if not result.success:
        raise RuntimeError(f"K8s deploy failed for {iteration_path}; see deploy.json")


__all__ = ["deploy_iteration", "prepare_iteration", "render_and_deploy", "render_iteration"]

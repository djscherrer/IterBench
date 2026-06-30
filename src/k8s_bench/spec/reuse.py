"""Copy spec.yaml from a prior iteration (no LLM). Called from :mod:`k8s_bench.stages.spec`."""

from __future__ import annotations

import logging
from pathlib import Path

from ..workspace import (
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    iteration_spec_dir,
    iteration_spec_path,
    normalize_iteration_id,
    resolve_iteration_dir,
    default_k8s_namespace,
)
from .models import K8sWorkloadSpec
from .render import render_iteration

def reuse_deployment_spec_for_iteration(
    *,
    iteration_path: Path,
    sample_dir: Path,
    source_iteration_id: str,
    target_iteration_id: str,
    extra_labels: dict[str, str] | None = None,
    logger: logging.Logger,
    experiment_id: str | None = None,
) -> Path:
    """
    Copy deployment parameters from a prior iteration (no spec LLM).

    Used after successful **code** refinement: bench the new image under the
    same replicas/resources/DB settings as the iteration we learned from.
    """
    source_path = resolve_iteration_dir(
        sample_dir, source_iteration_id, experiment_id=experiment_id
    )
    src_spec_path = find_iteration_spec_path(source_path)
    if src_spec_path is None:
        raise FileNotFoundError(
            f"No spec to reuse under {source_path} (from {source_iteration_id!r})"
        )

    spec = K8sWorkloadSpec.from_yaml_file(src_spec_path)
    iid = normalize_iteration_id(target_iteration_id)
    labels = dict(spec.labels)
    if extra_labels:
        labels.update(extra_labels)

    reused = K8sWorkloadSpec(
        iteration_id=iid,
        namespace=default_k8s_namespace(iid, experiment_id=experiment_id),
        backend=spec.backend,
        database=spec.database,
        pooler=spec.pooler,
        read_pooler=spec.read_pooler,
        cache=spec.cache,
        labels=labels,
    )

    ensure_iteration_core_layout(iteration_path)
    dest = iteration_spec_path(iteration_path)
    reused.write_yaml(dest)
    render_iteration(iteration_path)

    note = (
        f"Reused deployment spec from {source_path.name} ({source_iteration_id})\n"
        f"Target iteration: {iid}\n"
        "No LLM spec generation (code-only refinement phase).\n"
    )
    (iteration_spec_dir(iteration_path) / "reused_from.txt").write_text(
        note, encoding="utf-8"
    )
    logger.info(
        "Reused deployment spec from %s → %s (no LLM spec generation)",
        source_path,
        dest,
    )
    return dest



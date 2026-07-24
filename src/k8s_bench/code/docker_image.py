"""Resolve or build local Docker images for k8s bench and deploy-only paths."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import docker


def ensure_docker_image(
    task: Any,
    results_dir: Path,
    sample: int,
    image_id: str | None,
    logger: logging.Logger,
    *,
    code_dir: Path | None = None,
) -> str | None:
    """
    Resolve a Docker image id for bench/deploy.

    Reuses ``image_id`` when it is still present locally **unless** ``code_dir``
    points at an iteration-local snapshot (``iterations/.../02-code/code/``),
    which differs from the sample-level ``code/`` baseline. That case always
    triggers a fresh build so hand-edited or LLM-refined code is picked up.
    """
    sample_code_dir = task.get_code_dir(results_dir, sample)
    iteration_snapshot = (
        code_dir is not None
        and code_dir.is_dir()
        and code_dir.resolve() != sample_code_dir.resolve()
    )
    if iteration_snapshot:
        logger.info("Building image from iteration code snapshot: %s", code_dir)
        return task._build_image_from_code_dir(code_dir, logger)

    if image_id:
        try:
            docker.from_env().images.get(image_id)
            return image_id
        except Exception:
            logger.warning(
                "Image %s found in logs but not in Docker. Rebuilding...",
                image_id,
            )

    if code_dir is not None and code_dir.is_dir():
        return task._build_image_from_code_dir(code_dir, logger)
    return task._build_image(results_dir, sample, logger)

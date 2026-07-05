"""
Single spec-generation attempt: LLM call, parse, validate, write artifacts.

Retry loops and phase failure handling live in :mod:`k8s_bench.stages.spec`.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from env.base import Env
from llm import Prompter
from llm.conversation import send_with_retries
from scenarios.base import Scenario

from ..cluster.capacity import ClusterCapacity
from ..feedback import IterationFeedback
from ..prompt_helpers import ArtifactPointers
from ..failure import SpecFailureRecord
from ..failure.persist import spec_attempt_dir
from ..workspace import (
    PROMPT_LOG_FILENAME,
    RESPONSE_LOG_FILENAME,
    attempt_subdir,
    ensure_iteration_core_layout,
    iteration_spec_attempts_dir,
    iteration_spec_dir,
    iteration_spec_path,
)
from .models import BackendSpec, K8sWorkloadSpec
from .parse import merge_fragment_into_spec, parse_spec_fragment
from .placement import normalize_spec_placement
from .prompts import build_k8s_spec_prompt
from .render import render_iteration
from .validate import SpecValidationError, validate_spec_against_cluster


@dataclass(frozen=True)
class SpecAttemptResult:
    """Outcome of one spec attempt (LLM + validate + write)."""

    spec_path: Path | None = None
    error: str | None = None
    spec: K8sWorkloadSpec | None = None
    raw_response: str = ""
    warnings: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    validation_warnings: tuple[str, ...] = ()
    failure: SpecFailureRecord | None = None


_SPEC_ATTEMPT_META_FILENAME = "attempt.json"


def rotate_top_level_into_attempt(
    iteration_path: pathlib.Path,
    attempt_dir: pathlib.Path,
) -> None:
    """Move the current ``03-spec/`` snapshot into ``attempts/<NNN>/`` before retry."""
    spec_dir = iteration_spec_dir(iteration_path)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name in (PROMPT_LOG_FILENAME, RESPONSE_LOG_FILENAME, "spec_gen.json", "spec.yaml"):
        src = spec_dir / name
        if src.is_file():
            shutil.move(str(src), str(attempt_dir / name))
    for sub in ("manifests",):
        src = spec_dir / sub
        if src.is_dir():
            dest = attempt_dir / sub
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))


def _spec_attempt_dir(
    iteration_path: pathlib.Path, *, attempt_index: int, enable_attempts: bool
) -> Path:
    if enable_attempts:
        return attempt_subdir(iteration_spec_attempts_dir(iteration_path), attempt_index)
    return spec_attempt_dir(iteration_path, attempt_index)


def _record_failed_spec_attempt(
    *,
    iteration_path: pathlib.Path,
    attempt_index: int,
    enable_attempts: bool,
    status: str,
    error: str | None,
    validation_feedback: str | None = None,
) -> pathlib.Path:
    """Rotate top-level spec artifacts into ``attempts/<NNN>/`` and write meta."""
    attempt_dir = _spec_attempt_dir(
        iteration_path, attempt_index=attempt_index, enable_attempts=enable_attempts
    )
    if enable_attempts:
        rotate_top_level_into_attempt(iteration_path, attempt_dir)
    else:
        attempt_dir.mkdir(parents=True, exist_ok=True)
    _write_spec_attempt_meta(
        attempt_dir,
        attempt_index=attempt_index,
        status=status,
        error=error,
        validation_feedback=validation_feedback,
    )
    return attempt_dir


def persist_spec_attempt_failure_artifacts(
    *,
    iteration_path: pathlib.Path,
    attempt_index: int,
    enable_attempts: bool,
    failure: SpecFailureRecord | None,
    status: str,
    error: str | None,
    validation_feedback: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Rotate failed attempt artifacts (when enabled) and persist ``failure.json``."""
    attempt_dir = _record_failed_spec_attempt(
        iteration_path=iteration_path,
        attempt_index=attempt_index,
        enable_attempts=enable_attempts,
        status=status,
        error=error,
        validation_feedback=validation_feedback,
    )
    if failure is None:
        return
    log = logger or logging.getLogger(__name__)
    try:
        from ..failure.persist import write_spec_attempt_failure

        write_spec_attempt_failure(attempt_dir, failure)
    except Exception as exc:
        log.debug("could not persist spec attempt failure: %s", exc)


def _write_spec_attempt_meta(
    attempt_dir: pathlib.Path,
    *,
    attempt_index: int,
    status: str,
    error: str | None,
    validation_feedback: str | None,
    note: str | None = None,
) -> None:
    """Persist ``attempts/<NNN>/attempt.json`` (one outer spec attempt)."""
    payload: dict[str, Any] = {
        "attempt_index": attempt_index,
        "status": status,
        "error": error,
        "validation_feedback": validation_feedback,
    }
    if note:
        payload["note"] = note
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / _SPEC_ATTEMPT_META_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def record_spec_deploy_probe_failure(
    iteration_path: pathlib.Path,
    *,
    attempt_index: int,
    probe_reason: str,
    probe_feedback: str,
) -> None:
    """Mark an outer spec attempt as ``deploy_probe_failed`` after rotation."""
    attempt_dir = attempt_subdir(
        iteration_spec_attempts_dir(iteration_path), attempt_index
    )
    meta_path = attempt_dir / _SPEC_ATTEMPT_META_FILENAME
    if not meta_path.is_file():
        return
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    data["status"] = "deploy_probe_failed"
    data["probe_reason"] = probe_reason
    data["probe_feedback"] = probe_feedback
    meta_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def generate_k8s_workload_spec(
    *,
    env: Env,
    scenario: Scenario,
    safety_prompt: str,
    capacity: ClusterCapacity,
    iteration_id: str,
    logger: logging.Logger,
    session: "Prompter",
    prior_feedback: IterationFeedback | str | None = None,
    validation_feedback: str | None = None,
    max_validation_retries: int = 3,
    sample_dir: pathlib.Path | None = None,
    iteration_path: pathlib.Path | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
    artifact_pointers: ArtifactPointers | None = None,
    experiment_id: str = "default",
    llm_max_cost_usd: float | None = None,
    max_retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 128.0,
) -> tuple[K8sWorkloadSpec, str, list[str]]:
    """Call the configured LLM and return (spec, raw_response, validation_warnings).

    When ``enable_attempts`` is ``True`` on the outer :func:`run_spec_attempt`,
    failed attempts are rotated into ``03-spec/attempts/<NNN>/`` before retry.
    This function always writes prompt/response to the phase top level.
    """
    last_raw = ""
    validation_hint = validation_feedback
    last_errors: list[str] = []
    last_warnings: list[str] = []
    for attempt in range(1, max_validation_retries + 1):
        prompt = build_k8s_spec_prompt(
            env=env,
            scenario=scenario,
            safety_prompt=safety_prompt,
            capacity=capacity,
            iteration_id=iteration_id,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            refinement=prior_feedback is not None,
            validation_feedback=validation_hint,
            artifact_pointers=artifact_pointers,
        )

        # Write prompt/response at the phase top level; failed outer attempts are
        # rotated into ``attempts/<NNN>/`` by the stage loop (move-on-fail).
        if iteration_path is not None:
            spec_dir = iteration_spec_dir(iteration_path)
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / PROMPT_LOG_FILENAME).write_text(
                prompt + "\n", encoding="utf-8"
            )
        if sample_dir is not None:
            from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

            check_k8s_llm_budget(
                sample_dir,
                experiment_id=experiment_id,
                max_cost_usd=llm_max_cost_usd,
            )

        empty_response_error = "LLM returned no completion for k8s spec generation"
        try:
            last_raw = send_with_retries(
                session,
                prompt,
                logger,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
                log_label="K8s spec generation",
            )
        except RuntimeError as exc:
            if "no completion" in str(exc).lower():
                raise RuntimeError(empty_response_error) from exc
            raise
        if sample_dir is not None:
            record_k8s_llm_call(
                prompter=session,
                call_type="k8s_spec_generation",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=(
                    iteration_spec_dir(iteration_path) if iteration_path else None
                ),
                iteration_id=iteration_id,
                note=f"validation_round={attempt}",
                experiment_id=experiment_id,
                max_cost_usd=llm_max_cost_usd,
            )
        if iteration_path is not None:
            (iteration_spec_dir(iteration_path) / RESPONSE_LOG_FILENAME).write_text(
                last_raw + "\n", encoding="utf-8"
            )
        try:
            fragment = parse_spec_fragment(last_raw)
            spec = merge_fragment_into_spec(
                fragment,
                iteration_id=iteration_id,
                app_port=env.port,
                needs_db=scenario.needs_db,
                labels={},
                experiment_id=experiment_id,
            )
        except ValueError as parse_exc:
            validation_hint = (
                f"Your previous response could not be parsed as a <SPEC> "
                f"YAML fragment: {parse_exc}. Re-emit the spec inside a "
                "single <SPEC>...</SPEC> block."
            )
            logger.warning(
                "spec validation attempt %d/%d failed (parse): %s",
                attempt,
                max_validation_retries,
                parse_exc,
            )
            continue

        # Resolve placement names in the spec.
        spec, placement_errors = normalize_spec_placement(spec, capacity)
        if placement_errors:
            validation_hint = SpecValidationError(placement_errors).to_prompt_text()
            logger.warning(
                "spec validation attempt %d/%d failed (placement): %s",
                attempt,
                max_validation_retries,
                placement_errors,
            )
            continue

        # Validate the spec against the cluster capacity and scheduling rules.
        result = validate_spec_against_cluster(spec, capacity)
        if result.errors:
            last_errors = list(result.errors)
            last_warnings = list(result.warnings)
            validation_hint = SpecValidationError(result.errors, result.warnings).to_prompt_text()
            logger.warning(
                "spec validation attempt %d/%d failed: %s",
                attempt,
                max_validation_retries,
                result.errors,
            )
            continue

        if attempt > 1:
            logger.info("spec validation passed on attempt %d", attempt)
        return spec, last_raw, result.warnings

    if last_errors:
        raise SpecValidationError(
            [f"Spec still invalid after {max_validation_retries} generation attempt(s)."]
            + last_errors,
            last_warnings,
        )
    raise SpecValidationError(
        [f"Spec still invalid after {max_validation_retries} generation attempt(s)."]
        + [ln for ln in (validation_hint or "").splitlines() if ln.strip()][-8:],
        last_warnings,
    )


def write_spec_generation_artifacts(
    iteration_path: pathlib.Path,
    *,
    spec: K8sWorkloadSpec,
    raw_response: str,
    capacity: ClusterCapacity,
    warnings: list[str],
    logger: logging.Logger,
) -> pathlib.Path:
    ensure_iteration_core_layout(iteration_path)
    spec_path = iteration_spec_path(iteration_path)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec.write_yaml(spec_path)
    render_iteration(iteration_path)

    meta = {
        "spec_path": str(spec_path),
        "warnings": warnings,
        "cluster_capacity": capacity.to_prompt_dict(),
        "workload_spec": spec.to_yaml_dict(),
    }
    spec_dir = iteration_spec_dir(iteration_path)
    (spec_dir / "spec_gen.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (spec_dir / RESPONSE_LOG_FILENAME).write_text(
        raw_response + "\n",
        encoding="utf-8",
    )
    if warnings:
        for w in warnings:
            logger.warning("spec validation: %s", w)
    logger.info("Wrote %s and rendered manifests", spec_path)
    return spec_path


def _apply_task_labels_to_spec(
    spec: K8sWorkloadSpec,
    *,
    task: Any,
    results_dir: Path,
    sample: int,
) -> K8sWorkloadSpec:
    from tasks import esc

    labels = {
        "baxbench.dev/model": esc(task.model),
        "baxbench.dev/scenario": esc(task.scenario.id),
        "baxbench.dev/env": esc(task.env.id),
        "baxbench.dev/spec-gen": "true",
    }
    return K8sWorkloadSpec(
        iteration_id=spec.iteration_id,
        namespace=spec.namespace,
        backend=BackendSpec.from_mapping(
            {
                "image": spec.backend.image,
                "replicas": spec.backend.replicas,
                "port": task.env.port,
                "resources": {
                    "cpu_request": spec.backend.resources.cpu_request,
                    "cpu_limit": spec.backend.resources.cpu_limit,
                    "memory_request": spec.backend.resources.memory_request,
                    "memory_limit": spec.backend.resources.memory_limit,
                },
                "env": dict(spec.backend.env),
                "placement": {
                    "workers": list(spec.backend.placement_workers),
                    "spread_replicas": spec.backend.spread_replicas,
                },
            }
        ),
        database=spec.database,
        pooler=spec.pooler,
        read_pooler=spec.read_pooler,
        cache=spec.cache,
        labels={**spec.labels, **labels},
    )


def run_spec_attempt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    iteration_id: str,
    session: "Prompter",
    logger: logging.Logger,
    capacity: ClusterCapacity,
    prior_feedback: IterationFeedback | None = None,
    validation_feedback: str | None = None,
    max_validation_retries: int = 1,
    attempt_index: int = 1,
    iteration_index: int = 0,
    total_iterations: int = 0,
    enable_attempts: bool = False,
    experiment_id: str = "default",
    llm_max_cost_usd: float | None = None,
    max_retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 128.0,
) -> SpecAttemptResult:
    """
    Run one spec generation attempt: LLM → parse → validate → write artifacts.

    Returns :class:`SpecAttemptResult` with ``spec_path`` on success or
    ``error`` when static validation fails after ``max_validation_retries``.
    """
    sample_dir = task.get_sample_dir(results_dir, sample)
    from ..prompt_helpers import resolve_artifact_pointers
    from ..session import persist_session

    artifact_pointers = (
        resolve_artifact_pointers(sample_dir, experiment_id=experiment_id)
        if prior_feedback is not None
        else None
    )
    try:
        spec, raw, warnings = generate_k8s_workload_spec(
            env=task.env,
            scenario=task.scenario,
            safety_prompt=task.safety_prompt,
            capacity=capacity,
            iteration_id=iteration_id,
            logger=logger,
            prior_feedback=prior_feedback,
            validation_feedback=validation_feedback,
            max_validation_retries=max_validation_retries,
            sample_dir=sample_dir,
            iteration_path=iteration_path,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            session=session,
            artifact_pointers=artifact_pointers,
            experiment_id=experiment_id,
            llm_max_cost_usd=llm_max_cost_usd,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
        )
        persist_session(
            session, sample_dir, experiment_id=experiment_id, logger=logger
        )
        spec = _apply_task_labels_to_spec(
            spec, task=task, results_dir=results_dir, sample=sample
        )
        out = write_spec_generation_artifacts(
            iteration_path,
            spec=spec,
            raw_response=raw,
            capacity=capacity,
            warnings=warnings,
            logger=logger,
        )
        return SpecAttemptResult(
            spec_path=out,
            spec=spec,
            raw_response=raw,
            warnings=tuple(warnings),
        )
    except SpecValidationError as exc:
        failure = SpecFailureRecord(
            phase="spec",
            kind="spec_validation",
            iteration_id=iteration_id,
            attempt=attempt_index,
            summary="static spec validation failed",
            errors=tuple(exc.errors),
            warnings=tuple(getattr(exc, "warnings", []) or ()),
        )
        persist_spec_attempt_failure_artifacts(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            failure=failure,
            status="validation_failed",
            error=exc.to_prompt_text(),
            validation_feedback=validation_feedback,
            logger=logger,
        )
        return SpecAttemptResult(
            error=exc.to_prompt_text(),
            validation_errors=tuple(exc.errors),
            validation_warnings=tuple(getattr(exc, "warnings", []) or ()),
            failure=failure,
        )
    except Exception as exc:
        logger.exception("spec generation failed: %s", exc, exc_info=exc)
        msg = str(exc)
        failure = SpecFailureRecord(
            phase="spec",
            kind="llm_call",
            iteration_id=iteration_id,
            attempt=attempt_index,
            summary="spec generation failed",
            llm_error=msg or "spec generation failed",
        )
        persist_spec_attempt_failure_artifacts(
            iteration_path=iteration_path,
            attempt_index=attempt_index,
            enable_attempts=enable_attempts,
            failure=failure,
            status="llm_failed",
            error=msg,
            validation_feedback=validation_feedback,
            logger=logger,
        )
        return SpecAttemptResult(error=msg, failure=failure)


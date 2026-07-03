"""Baseline codegen artifact I/O (``codegen.json``, attempt rotation)."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .docker_image import ensure_docker_image
from .shared import functional_tests_passed_at
from ..workspace import (
    PROMPT_LOG_FILENAME,
    RESPONSE_LOG_FILENAME,
    attempt_subdir,
    baseline_codegen_meta_path,
    image_id_from_test_log,
    iteration_code_attempts_dir,
    iteration_code_phase_dir,
    iteration_code_snapshot_dir,
    iteration_functional_tests_dir,
    next_attempt_index,
)
_ATTEMPT_META_FILENAME = "attempt.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def existing_passing_codegen(iteration_path: Path) -> tuple[Path, str] | None:
    meta_path = baseline_codegen_meta_path(iteration_path)
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict) or meta.get("status") != "passed":
        return None
    code_dir = iteration_code_snapshot_dir(iteration_path)
    if not code_dir.is_dir() or not any(code_dir.iterdir()):
        return None
    ft_results = iteration_functional_tests_dir(iteration_path) / "test_results.json"
    if not functional_tests_passed_at(ft_results):
        return None
    test_log = iteration_functional_tests_dir(iteration_path) / "test.log"
    image_id = image_id_from_test_log(test_log)
    if image_id is None:
        return None
    return code_dir, image_id


def try_reuse_baseline_codegen(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
) -> tuple[Path, str] | None:
    """Return ``(code_dir, image_id)`` when a passing baseline codegen can be reused."""
    existing = existing_passing_codegen(iteration_path)
    if existing is None:
        return None
    code_dir, image_id = existing
    sample_logger = logging.getLogger(task.id)
    resolved = ensure_docker_image(
        task,
        results_dir,
        sample,
        image_id,
        sample_logger,
        code_dir=code_dir,
    )
    if resolved is None:
        return None
    sample_logger.info(
        "Baseline codegen: reusing existing passing iteration-000 (image=%s)",
        resolved,
    )
    return code_dir, resolved


def rotate_top_level_into_attempt(
    iteration_path: Path,
    attempt_dir: Path,
) -> None:
    phase_dir = iteration_code_phase_dir(iteration_path)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name in (PROMPT_LOG_FILENAME, RESPONSE_LOG_FILENAME):
        src = phase_dir / name
        if src.is_file():
            shutil.move(str(src), str(attempt_dir / name))
    for sub in ("code", "functional_tests"):
        src = phase_dir / sub
        if src.is_dir():
            dest = attempt_dir / sub
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))


def write_attempt_meta(
    attempt_dir: Path,
    *,
    attempt_index: int,
    status: str,
    error: str | None,
    num_passed_ft: int | None,
    num_total_ft: int | None,
    duration_s: float,
    note: str | None = None,
    infra_failure: bool = False,
    error_excerpt: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "attempt_index": attempt_index,
        "status": status,
        "error": error,
        "num_passed_ft": num_passed_ft,
        "num_total_ft": num_total_ft,
        "duration_s": round(duration_s, 3),
        "finished_at": utc_now(),
    }
    if note:
        payload["note"] = note
    if infra_failure:
        payload["infra_failure"] = True
    if error_excerpt:
        payload["error_excerpt"] = error_excerpt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / _ATTEMPT_META_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_attempt_meta_for_summary(attempts_dir: Path) -> list[dict[str, Any]]:
    if not attempts_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    entries: list[tuple[int, Path]] = []
    for child in attempts_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            n = int(child.name)
        except ValueError:
            continue
        entries.append((n, child))
    for _, attempt_dir in sorted(entries):
        meta_path = attempt_dir / _ATTEMPT_META_FILENAME
        if not meta_path.is_file():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def write_codegen_meta(
    iteration_path: Path,
    *,
    status: str,
    attempts_used: int,
    max_attempts: int,
    task: Any,
    winning_attempt: int | None,
    error: str | None = None,
    infra_failure: bool = False,
) -> Path:
    attempts = read_attempt_meta_for_summary(iteration_code_attempts_dir(iteration_path))
    payload: dict[str, Any] = {
        "status": status,
        "attempts_used": attempts_used,
        "max_attempts": max_attempts,
        "winning_attempt": winning_attempt,
        "error": error,
        "model": task.model,
        "provider": task.provider,
        "temperature": task.temperature,
        "reasoning_effort": task.reasoning_effort,
        "spec_type": task.spec_type,
        "safety_prompt": task.safety_prompt,
        "scenario": task.scenario.id,
        "env": task.env.id,
        "use_stubs": task.use_stubs,
        "finished_at": utc_now(),
        "attempts": attempts,
    }
    if infra_failure:
        payload["infra_failure"] = True
    path = baseline_codegen_meta_path(iteration_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def reset_baseline_phase_on_force(iteration_path: Path) -> None:
    phase_dir = iteration_code_phase_dir(iteration_path)
    attempts_root = iteration_code_attempts_dir(iteration_path)
    if attempts_root.is_dir():
        shutil.rmtree(attempts_root)
    for child in phase_dir.iterdir():
        if child.name == "attempts":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

def append_baseline_summary(
    *,
    sample_dir: Path,
    iteration_path: Path,
    task: Any,
    attempts_used: int,
    max_attempts: int,
    winning_attempt: int | None,
    status: str,
    error: str | None,
    logger: logging.Logger,
) -> None:
    try:
        from ..experiment_summary import append_baseline_codegen_block

        append_baseline_codegen_block(
            sample_dir=sample_dir,
            iteration_path=iteration_path,
            task=task,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            winning_attempt=winning_attempt,
            status=status,
            error=error,
        )
    except Exception as exc:
        logger.warning(
            "Could not append baseline codegen block to experiment summary: %s",
            exc,
        )

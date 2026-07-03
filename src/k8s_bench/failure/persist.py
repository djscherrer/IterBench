"""Read/write attempt- and iteration-scoped failure artifacts."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..workspace.paths import (
    attempt_subdir,
    iteration_code_attempts_dir,
    iteration_code_phase_dir,
    iteration_deploy_dir,
    iteration_spec_dir,
)
from .record import FailureRecord, IterationFailure, Phase

FAILURE_FILENAME = "failure.json"
LEGACY_FAILURE_REPORT_FILENAME = "failure_report.json"


def phase_dir_for(iteration_path: Path, phase: Phase) -> Path:
    if phase == "deploy":
        return iteration_deploy_dir(iteration_path)
    if phase == "spec":
        return iteration_spec_dir(iteration_path)
    return iteration_code_phase_dir(iteration_path)


def iteration_failure_path(iteration_path: Path, phase: Phase) -> Path:
    return phase_dir_for(iteration_path, phase) / FAILURE_FILENAME


def attempt_failure_path(attempt_dir: Path) -> Path:
    return attempt_dir / FAILURE_FILENAME


def write_attempt_failure(attempt_dir: Path, record: FailureRecord) -> Path:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    out = attempt_failure_path(attempt_dir)
    out.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def load_attempt_failure(attempt_dir: Path) -> FailureRecord | None:
    path = attempt_failure_path(attempt_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return FailureRecord.from_dict(data)


def write_iteration_failure(
    iteration_path: Path,
    failure: IterationFailure,
) -> Path:
    out = iteration_failure_path(iteration_path, failure.phase)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(failure.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def load_iteration_failure(
    iteration_path: Path,
    phase: Phase,
) -> IterationFailure | None:
    path = iteration_failure_path(iteration_path, phase)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and "terminal" in data:
            return IterationFailure.from_dict(data)
    legacy = _load_legacy_code_failure(iteration_path)
    if legacy is not None and phase == "code":
        return IterationFailure(
            iteration_id=legacy.iteration_id,
            phase="code",
            terminal=legacy,
            terminal_attempt=legacy.attempt,
            attempts={legacy.attempt: legacy} if legacy.attempt is not None else {},
        )
    return None


def load_terminal_failure_record(
    iteration_path: Path,
    *,
    phase: Phase | None = None,
) -> FailureRecord | None:
    """Return the terminal :class:`FailureRecord` for an iteration phase."""
    phases: tuple[Phase, ...]
    if phase is not None:
        phases = (phase,)
    else:
        from ..workspace.meta import read_iteration_meta

        meta = read_iteration_meta(iteration_path) or {}
        raw_kind = str(meta.get("failure_kind") or "code")
        phases = (raw_kind,) if raw_kind in {"code", "spec", "deploy"} else ("code", "spec", "deploy")  # type: ignore[assignment]
    for ph in phases:
        loaded = load_iteration_failure(iteration_path, ph)
        if loaded is not None:
            return loaded.terminal
    return _load_legacy_code_failure(iteration_path)


def _load_legacy_code_failure(iteration_path: Path) -> FailureRecord | None:
    path = iteration_code_phase_dir(iteration_path) / LEGACY_FAILURE_REPORT_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return FailureRecord.from_legacy_functional_report(data)


def code_attempt_dir(iteration_path: Path, attempt_index: int) -> Path:
    return attempt_subdir(iteration_code_attempts_dir(iteration_path), attempt_index)


def load_prior_code_attempt_failure(
    iteration_path: Path,
    attempt_index: int,
) -> FailureRecord | None:
    if attempt_index <= 1:
        return None
    return load_attempt_failure(code_attempt_dir(iteration_path, attempt_index - 1))


def collect_code_attempt_failures(
    iteration_path: Path,
    *,
    logger: logging.Logger | None = None,
) -> dict[int, FailureRecord]:
    log = logger or logging.getLogger(__name__)
    attempts_dir = iteration_code_attempts_dir(iteration_path)
    if not attempts_dir.is_dir():
        return {}
    out: dict[int, FailureRecord] = {}
    for child in sorted(attempts_dir.iterdir()):
        if not child.is_dir():
            continue
        m = re.match(r"^(\d+)$", child.name)
        if not m:
            continue
        idx = int(m.group(1))
        record = load_attempt_failure(child)
        if record is None:
            log.debug("no failure.json in %s", child)
            continue
        out[idx] = record
    return out


def build_code_iteration_failure(
    iteration_path: Path,
    *,
    iteration_id: str,
    terminal_attempt: int | None,
    logger: logging.Logger | None = None,
) -> IterationFailure:
    from .build import build_code_failure_record

    attempts = collect_code_attempt_failures(iteration_path, logger=logger)
    terminal: FailureRecord | None = None
    if terminal_attempt is not None and terminal_attempt in attempts:
        terminal = attempts[terminal_attempt]
    if terminal is None and attempts:
        last_idx = max(attempts)
        terminal = attempts[last_idx]
        terminal_attempt = last_idx
    if terminal is None:
        terminal = build_code_failure_record(
            iteration_path,
            iteration_id=iteration_id,
            attempt=terminal_attempt,
            logger=logger,
        )
    return IterationFailure(
        iteration_id=iteration_id,
        phase="code",
        terminal=terminal,
        terminal_attempt=terminal_attempt,
        attempts=attempts,
    )

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
from .record import (
    BenchFailureRecord,
    CodeFailureRecord,
    DeployFailureRecord,
    IterationFailure,
    Phase,
    SpecFailureRecord,
    failure_record_from_dict,
)

FAILURE_FILENAME = "failure.json"


def phase_dir_for(iteration_path: Path, phase: Phase) -> Path:
    if phase == "deploy":
        return iteration_deploy_dir(iteration_path)
    if phase == "spec":
        return iteration_spec_dir(iteration_path)
    if phase == "bench":
        from ..workspace.paths import iteration_bench_dir

        return iteration_bench_dir(iteration_path)
    return iteration_code_phase_dir(iteration_path)


def iteration_failure_path(iteration_path: Path, phase: Phase) -> Path:
    return phase_dir_for(iteration_path, phase) / FAILURE_FILENAME


def attempt_failure_path(attempt_dir: Path) -> Path:
    return attempt_dir / FAILURE_FILENAME


def write_attempt_failure(attempt_dir: Path, record: CodeFailureRecord) -> Path:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    out = attempt_failure_path(attempt_dir)
    out.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def write_spec_attempt_failure(attempt_dir: Path, record: SpecFailureRecord) -> Path:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    out = attempt_failure_path(attempt_dir)
    out.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def load_spec_attempt_failure(attempt_dir: Path) -> SpecFailureRecord | None:
    path = attempt_failure_path(attempt_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return SpecFailureRecord.from_dict(data)


def spec_attempt_dir(iteration_path: Path, attempt_index: int) -> Path:
    from ..workspace.paths import iteration_spec_attempts_dir

    return attempt_subdir(iteration_spec_attempts_dir(iteration_path), attempt_index)


def load_prior_spec_attempt_failure(
    iteration_path: Path,
    attempt_index: int,
) -> SpecFailureRecord | None:
    if attempt_index <= 1:
        return None
    return load_spec_attempt_failure(spec_attempt_dir(iteration_path, attempt_index - 1))


def collect_spec_attempt_failures(
    iteration_path: Path,
    *,
    logger: logging.Logger | None = None,
) -> dict[int, SpecFailureRecord]:
    from ..workspace.paths import iteration_spec_attempts_dir

    log = logger or logging.getLogger(__name__)
    attempts_dir = iteration_spec_attempts_dir(iteration_path)
    if not attempts_dir.is_dir():
        return {}
    out: dict[int, SpecFailureRecord] = {}
    for child in sorted(attempts_dir.iterdir()):
        if not child.is_dir():
            continue
        m = re.match(r"^(\d+)$", child.name)
        if not m:
            continue
        idx = int(m.group(1))
        record = load_spec_attempt_failure(child)
        if record is None:
            log.debug("no failure.json in %s", child)
            continue
        out[idx] = record
    return out


def load_attempt_failure(attempt_dir: Path) -> CodeFailureRecord | None:
    path = attempt_failure_path(attempt_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return CodeFailureRecord.from_dict(data)


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
    return None


def load_terminal_failure_record(
    iteration_path: Path,
    *,
    phase: Phase | None = None,
) -> object | None:
    """Return the terminal typed failure record for an iteration phase."""
    phases: tuple[Phase, ...]
    if phase is not None:
        phases = (phase,)
    else:
        from ..workspace.meta import read_iteration_meta

        meta = read_iteration_meta(iteration_path) or {}
        raw_kind = str(meta.get("failure_kind") or "code")
        phases = (
            (raw_kind,)
            if raw_kind in {"code", "spec", "deploy", "bench"}
            else ("code", "spec", "deploy", "bench")
        )  # type: ignore[assignment]
    for ph in phases:
        loaded = load_iteration_failure(iteration_path, ph)
        if loaded is not None:
            return loaded.terminal
    return None


def load_prior_iteration_failure(
    sample_dir: Path,
    iteration_index: int,
    *,
    experiment_id: str | None = None,
) -> IterationFailure | None:
    """Load the failure envelope from iteration N−1 when that iteration failed."""
    if iteration_index <= 0:
        return None

    from ..workspace import (
        iteration_id_for_index,
        iteration_is_failed,
        read_iteration_meta,
        resolve_iteration_dir,
    )

    prev_idx = iteration_index - 1
    prev_id = iteration_id_for_index(prev_idx)
    iteration_path = resolve_iteration_dir(
        sample_dir, prev_id, experiment_id=experiment_id
    )
    if not iteration_is_failed(iteration_path):
        return None

    meta = read_iteration_meta(iteration_path) or {}
    raw_kind = str(meta.get("failure_kind") or "")
    if raw_kind in {"code", "spec", "deploy", "bench"}:
        loaded = load_iteration_failure(iteration_path, raw_kind)  # type: ignore[arg-type]
        if loaded is not None:
            return loaded

    for phase in ("code", "spec", "deploy", "bench"):
        loaded = load_iteration_failure(iteration_path, phase)  # type: ignore[arg-type]
        if loaded is not None:
            return loaded
    return None


def code_attempt_dir(iteration_path: Path, attempt_index: int) -> Path:
    return attempt_subdir(iteration_code_attempts_dir(iteration_path), attempt_index)


def load_prior_code_attempt_failure(
    iteration_path: Path,
    attempt_index: int,
) -> CodeFailureRecord | None:
    if attempt_index <= 1:
        return None
    return load_attempt_failure(code_attempt_dir(iteration_path, attempt_index - 1))


def collect_code_attempt_failures(
    iteration_path: Path,
    *,
    logger: logging.Logger | None = None,
) -> dict[int, CodeFailureRecord]:
    log = logger or logging.getLogger(__name__)
    attempts_dir = iteration_code_attempts_dir(iteration_path)
    if not attempts_dir.is_dir():
        return {}
    out: dict[int, CodeFailureRecord] = {}
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
    terminal: CodeFailureRecord | None = None
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


def build_spec_iteration_failure(
    iteration_path: Path,
    *,
    iteration_id: str,
    terminal_attempt: int | None,
    fallback: SpecFailureRecord | None = None,
    logger: logging.Logger | None = None,
) -> IterationFailure:
    attempts = collect_spec_attempt_failures(iteration_path, logger=logger)
    terminal: SpecFailureRecord | None = None
    if terminal_attempt is not None and terminal_attempt in attempts:
        terminal = attempts[terminal_attempt]
    if terminal is None and attempts:
        last_idx = max(attempts)
        terminal = attempts[last_idx]
        terminal_attempt = last_idx
    if terminal is None:
        terminal = fallback or SpecFailureRecord(
            phase="spec",
            kind="spec_validation",
            iteration_id=iteration_id,
            summary="spec phase failed",
        )
    return IterationFailure(
        iteration_id=iteration_id,
        phase="spec",
        terminal=terminal,
        terminal_attempt=terminal_attempt,
        attempts=attempts,
    )


def deploy_attempt_dir(iteration_path: Path, attempt_index: int) -> Path:
    from ..workspace.paths import iteration_deploy_attempts_dir

    return attempt_subdir(iteration_deploy_attempts_dir(iteration_path), attempt_index)


def bench_attempt_dir(iteration_path: Path, attempt_index: int) -> Path:
    from ..workspace.paths import iteration_bench_attempts_dir

    return attempt_subdir(iteration_bench_attempts_dir(iteration_path), attempt_index)


def write_deploy_attempt_failure(
    attempt_dir: Path, record: DeployFailureRecord
) -> Path:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    out = attempt_failure_path(attempt_dir)
    out.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def write_bench_attempt_failure(attempt_dir: Path, record: BenchFailureRecord) -> Path:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    out = attempt_failure_path(attempt_dir)
    out.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def persist_deploy_attempt_failure(
    *,
    iteration_path: Path,
    attempt_index: int,
    enable_attempts: bool,
    record: DeployFailureRecord,
    logger: logging.Logger | None = None,
) -> None:
    from ..stages.deploy import rotate_top_level_into_attempt

    attempt_dir = deploy_attempt_dir(iteration_path, attempt_index)
    if enable_attempts:
        rotate_top_level_into_attempt(iteration_path, attempt_dir)
    else:
        attempt_dir.mkdir(parents=True, exist_ok=True)
    log = logger or logging.getLogger(__name__)
    try:
        write_deploy_attempt_failure(attempt_dir, record)
    except Exception as exc:
        log.warning(
            "could not persist deploy attempt failure for %s attempt %d: %s",
            record.iteration_id,
            attempt_index,
            exc,
        )


def persist_bench_attempt_failure(
    *,
    iteration_path: Path,
    attempt_index: int,
    enable_attempts: bool,
    record: BenchFailureRecord,
    logger: logging.Logger | None = None,
) -> None:
    from ..stages.bench import rotate_top_level_into_attempt

    attempt_dir = bench_attempt_dir(iteration_path, attempt_index)
    if enable_attempts:
        rotate_top_level_into_attempt(iteration_path, attempt_dir)
    else:
        attempt_dir.mkdir(parents=True, exist_ok=True)
    log = logger or logging.getLogger(__name__)
    try:
        write_bench_attempt_failure(attempt_dir, record)
    except Exception as exc:
        log.warning(
            "could not persist bench attempt failure for %s attempt %d: %s",
            record.iteration_id,
            attempt_index,
            exc,
        )


def collect_deploy_attempt_failures(
    iteration_path: Path,
    *,
    logger: logging.Logger | None = None,
) -> dict[int, DeployFailureRecord]:
    from ..workspace.paths import iteration_deploy_attempts_dir

    log = logger or logging.getLogger(__name__)
    attempts_dir = iteration_deploy_attempts_dir(iteration_path)
    if not attempts_dir.is_dir():
        return {}
    out: dict[int, DeployFailureRecord] = {}
    for child in sorted(attempts_dir.iterdir()):
        if not child.is_dir():
            continue
        m = re.match(r"^(\d+)$", child.name)
        if not m:
            continue
        idx = int(m.group(1))
        path = attempt_failure_path(child)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.debug("could not read %s", path)
            continue
        if isinstance(data, dict):
            out[idx] = DeployFailureRecord.from_dict(data)
    return out


def collect_bench_attempt_failures(
    iteration_path: Path,
    *,
    logger: logging.Logger | None = None,
) -> dict[int, BenchFailureRecord]:
    from ..workspace.paths import iteration_bench_attempts_dir

    log = logger or logging.getLogger(__name__)
    attempts_dir = iteration_bench_attempts_dir(iteration_path)
    if not attempts_dir.is_dir():
        return {}
    out: dict[int, BenchFailureRecord] = {}
    for child in sorted(attempts_dir.iterdir()):
        if not child.is_dir():
            continue
        m = re.match(r"^(\d+)$", child.name)
        if not m:
            continue
        idx = int(m.group(1))
        path = attempt_failure_path(child)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.debug("could not read %s", path)
            continue
        if isinstance(data, dict):
            out[idx] = BenchFailureRecord.from_dict(data)
    return out


def build_deploy_iteration_failure(
    iteration_path: Path,
    *,
    iteration_id: str,
    terminal_attempt: int | None = 1,
    fallback: DeployFailureRecord | None = None,
    logger: logging.Logger | None = None,
) -> IterationFailure:
    attempts = collect_deploy_attempt_failures(iteration_path, logger=logger)
    terminal: DeployFailureRecord | None = None
    if terminal_attempt is not None and terminal_attempt in attempts:
        terminal = attempts[terminal_attempt]
    if terminal is None and attempts:
        last_idx = max(attempts)
        terminal = attempts[last_idx]
        terminal_attempt = last_idx
    if terminal is None:
        terminal = fallback or DeployFailureRecord(
            phase="deploy",
            kind="deploy_probe",
            iteration_id=iteration_id,
            summary="deploy phase failed",
        )
    return IterationFailure(
        iteration_id=iteration_id,
        phase="deploy",
        terminal=terminal,
        terminal_attempt=terminal_attempt,
        attempts=attempts,
    )


def build_bench_iteration_failure(
    iteration_path: Path,
    *,
    iteration_id: str,
    terminal_attempt: int | None = 1,
    fallback: BenchFailureRecord | None = None,
    logger: logging.Logger | None = None,
) -> IterationFailure:
    attempts = collect_bench_attempt_failures(iteration_path, logger=logger)
    terminal: BenchFailureRecord | None = None
    if terminal_attempt is not None and terminal_attempt in attempts:
        terminal = attempts[terminal_attempt]
    if terminal is None and attempts:
        last_idx = max(attempts)
        terminal = attempts[last_idx]
        terminal_attempt = last_idx
    if terminal is None:
        terminal = fallback or BenchFailureRecord(
            phase="bench",
            kind="bench_run",
            iteration_id=iteration_id,
            summary="bench phase failed",
        )
    return IterationFailure(
        iteration_id=iteration_id,
        phase="bench",
        terminal=terminal,
        terminal_attempt=terminal_attempt,
        attempts=attempts,
    )

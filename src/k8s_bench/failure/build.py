"""Build :class:`FunctionalFailureReport` from functional-test log artifacts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..workspace.paths import iteration_functional_tests_dir
from .infra import detect_infrastructure_failure
from .models import FunctionalFailure, FunctionalFailureReport
from .patterns import (
    CONTAINER_ERROR_HINT_RE,
    FT_STATUS_RE,
    HARNESS_LINE_RE,
    INFRA_FAILURE_PATTERNS,
    PM2_NOISE_RE,
)
from .text import tail, trim

_PER_TEST_TAIL_LINES = 6
_CONTAINER_ERROR_TAIL_LINES = 14
_MAX_CONTAINER_ERROR_CHARS = 1600


def _read_test_results(ft_dir: Path) -> tuple[int, int]:
    path = ft_dir / "test_results.json"
    if not path.is_file():
        return 0, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0
    return int(data.get("num_passed_ft", 0) or 0), int(data.get("num_total_ft", 0) or 0)


def _scan_test_log_for_results(test_log: str) -> tuple[list[str], list[str]]:
    """Collect ordered (passed, failed) test names from ``test.log``."""
    passed: list[str] = []
    failed: list[str] = []
    for line in test_log.splitlines():
        m = FT_STATUS_RE.match(line.strip())
        if not m:
            continue
        if m.group("status") == "passed":
            passed.append(m.group("name"))
        else:
            failed.append(m.group("name"))
    return passed, failed


def _container_error_excerpt_for_test(
    test_log: str,
    failed_test_name: str,
) -> str:
    """Extract application error output from ``test.log`` for one failed test."""
    lines = test_log.splitlines()

    failed_idx: int | None = None
    for i, line in enumerate(lines):
        m = FT_STATUS_RE.match(line.strip())
        if m and m.group("name") == failed_test_name and m.group("status") == "failed":
            failed_idx = i
            break
    if failed_idx is None:
        return ""

    start = 0
    for i in range(failed_idx - 1, -1, -1):
        if "running functional test:" in lines[i]:
            start = i
            break

    section = lines[start:failed_idx]

    infra_evidence = ""
    for line in section:
        for _kind, pattern, _desc in INFRA_FAILURE_PATTERNS:
            if pattern.search(line):
                infra_evidence = line.strip()
                break
        if infra_evidence:
            break

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in section:
        if (
            HARNESS_LINE_RE.match(line)
            or not line.strip()
            or PM2_NOISE_RE.match(line)
        ):
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line.rstrip())
    if current:
        blocks.append(current)

    error_blocks = [
        b for b in blocks if any(CONTAINER_ERROR_HINT_RE.search(l) for l in b)
    ]
    chosen = error_blocks[-1] if error_blocks else (blocks[-1] if blocks else [])
    head = chosen[:_CONTAINER_ERROR_TAIL_LINES] if chosen else []
    body = "\n".join(head)
    if infra_evidence:
        body = (
            f"[infrastructure] {infra_evidence}\n\n{body}".rstrip()
            if body
            else f"[infrastructure] {infra_evidence}"
        )
    if not body:
        return ""
    return trim(body, max_chars=_MAX_CONTAINER_ERROR_CHARS)


def _generic_excerpt_from_test_log(test_log: str) -> str:
    if not test_log:
        return ""
    lines = test_log.splitlines()
    error_lines = [
        line
        for line in lines
        if CONTAINER_ERROR_HINT_RE.search(line)
        and not HARNESS_LINE_RE.match(line)
        and not PM2_NOISE_RE.match(line)
    ]
    if error_lines:
        return tail("\n".join(error_lines), max_lines=20, max_chars=1200)
    return tail("\n".join(lines), max_lines=20, max_chars=1200)


def build_functional_failure_report(
    iteration_path: Path,
    *,
    iteration_id: str | None = None,
    logger: logging.Logger | None = None,
) -> FunctionalFailureReport:
    """
    Inspect the iteration's ``functional_tests/`` directory and build a report.

    Tolerant of missing files: returns a best-effort report rather than raising.
    """
    log = logger or logging.getLogger(__name__)
    ft_dir = iteration_functional_tests_dir(iteration_path)
    iid = iteration_id or iteration_path.name

    passed_n, total_n = _read_test_results(ft_dir)

    test_log_path = ft_dir / "test.log"
    test_log = ""
    if test_log_path.is_file():
        try:
            test_log = test_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("Could not read %s: %s", test_log_path, exc)

    passed_names, failed_names = _scan_test_log_for_results(test_log)

    failures: list[FunctionalFailure] = []
    for name in failed_names:
        per_test_path = ft_dir / f"{name}.log"
        per_test_tail = ""
        if per_test_path.is_file():
            try:
                per_test_tail = tail(
                    per_test_path.read_text(encoding="utf-8", errors="replace"),
                    max_lines=_PER_TEST_TAIL_LINES,
                    max_chars=800,
                )
            except OSError as exc:
                log.debug("Could not read %s: %s", per_test_path, exc)
        container_excerpt = _container_error_excerpt_for_test(test_log, name)
        failures.append(
            FunctionalFailure(
                name=name,
                per_test_log_tail=per_test_tail,
                container_error_excerpt=container_excerpt,
            )
        )

    generic_excerpt = ""
    if not failures and (total_n == 0 or total_n > passed_n):
        generic_excerpt = _generic_excerpt_from_test_log(test_log)

    infra = detect_infrastructure_failure(test_log)
    if infra is not None:
        log.warning(
            "infrastructure failure detected for %s: %s (evidence: %s)",
            iid,
            infra.description,
            infra.evidence,
        )

    return FunctionalFailureReport(
        iteration_id=iid,
        num_passed_ft=passed_n,
        num_total_ft=total_n,
        failed_tests=tuple(failures),
        passed_tests=tuple(passed_names),
        generic_excerpt=generic_excerpt,
        infrastructure_failure=infra,
    )

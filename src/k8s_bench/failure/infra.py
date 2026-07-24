"""Detect infrastructure / harness failures that block functional tests."""

from __future__ import annotations

from pathlib import Path

from .record import CodeFailureRecord
from .patterns import (
    CONTAINER_LOGS_MARKER,
    HARNESS_LINE_RE,
    INFRA_FAILURE_PATTERNS,
)
from .text import trim


def container_logs_after_start_failure(test_log: str) -> str:
    """Return app stdout/stderr under the first ``container logs:`` after a start failure."""
    idx = test_log.find("Server did not start in time")
    if idx < 0:
        return ""
    after = test_log[idx:]
    logs_idx = after.find(CONTAINER_LOGS_MARKER)
    if logs_idx < 0:
        return ""
    body = after[logs_idx + len(CONTAINER_LOGS_MARKER) :]
    lines: list[str] = []
    for line in body.splitlines():
        if HARNESS_LINE_RE.match(line):
            if lines:
                break
            continue
        stripped = line.rstrip()
        if stripped and stripped != "(empty)":
            lines.append(stripped)
    return "\n".join(lines)


def startup_timeout_is_application_crash(test_log: str) -> bool:
    """True when a start-timeout was caused by the app crashing, not the harness."""
    if "container exited" in test_log and "Server did not start in time" in test_log:
        return True
    body = container_logs_after_start_failure(test_log)
    if not body:
        return False
    # Any non-empty container output after a start failure is treated as app-side
    # evidence (Go/Node/etc. often exit with a one-line fatal, not a Python traceback).
    return True


def detect_infrastructure_failure(test_log: str) -> CodeFailureRecord.InfrastructureFailure | None:
    """Return the first infrastructure failure marker in ``test.log``, if any."""
    if not test_log:
        return None
    for line in test_log.splitlines():
        for kind, pattern, description in INFRA_FAILURE_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            detail = description
            if kind == "server_did_not_start" and startup_timeout_is_application_crash(
                test_log
            ):
                continue
            if kind == "port_conflict":
                port = m.groupdict().get("port")
                if port:
                    detail = f"{description} (port {port})"
            return CodeFailureRecord.InfrastructureFailure(
                description=detail,
                evidence=trim(line.strip(), max_chars=600),
            )
    return None


def classify_ft_failure(ft_dir: Path) -> tuple[bool, str, str]:
    """Return ``(is_infra_failure, hint, log_excerpt)`` from a functional-tests dir."""
    log_path = ft_dir / "test.log"
    log_text = ""
    if log_path.is_file():
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
    excerpt = log_text[-2_000:] if log_text else ""
    infra = detect_infrastructure_failure(log_text)
    if infra is not None:
        return True, infra.description, excerpt
    return False, "", excerpt

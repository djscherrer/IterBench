"""Detect infrastructure / harness failures that block functional tests."""

from __future__ import annotations

from pathlib import Path

from .record import CodeFailureRecord
from .patterns import (
    APP_STARTUP_CRASH_RE,
    CONTAINER_LOGS_MARKER,
    INFRA_FAILURE_PATTERNS,
)
from .text import trim


def startup_timeout_is_application_crash(test_log: str) -> bool:
    """True when a start-timeout was caused by the app crashing, not the harness."""
    idx = test_log.find("Server did not start in time")
    if idx < 0:
        return False
    after = test_log[idx:]
    logs_idx = after.find(CONTAINER_LOGS_MARKER)
    if logs_idx < 0:
        return False
    section = after[logs_idx : logs_idx + 4_000]
    return bool(APP_STARTUP_CRASH_RE.search(section))


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

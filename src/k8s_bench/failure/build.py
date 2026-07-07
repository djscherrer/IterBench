"""Build :class:`CodeFailureRecord` from functional-test log artifacts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..workspace.paths import iteration_functional_tests_dir
from .record import CodeFailureRecord
from .infra import detect_infrastructure_failure, startup_timeout_is_application_crash
from .patterns import (
    APP_STARTUP_CRASH_RE,
    COMPILE_DIAGNOSTIC_RE,
    CONTAINER_ERROR_HINT_RE,
    CONTAINER_LOGS_MARKER,
    DOCKER_BUILD_FAILED_RE,
    FT_STATUS_RE,
    HARNESS_LINE_RE,
    INFRA_FAILURE_PATTERNS,
    PM2_NOISE_RE,
)
from .record import CodeFailureKind, CodeFailureRecord
from .text import filter_compile_diagnostics, strip_harness_noise, tail, trim

_PER_TEST_TAIL_LINES = 6
_CONTAINER_ERROR_TAIL_LINES = 14
_MAX_CONTAINER_ERROR_CHARS = 1600


def _app_crash_excerpt_from_section(section_text: str) -> str:
    """Extract application crash output from a per-test ``container logs:`` section."""
    if CONTAINER_LOGS_MARKER not in section_text:
        return ""
    after = section_text.split(CONTAINER_LOGS_MARKER, 1)[1]
    lines: list[str] = []
    for line in after.splitlines():
        if HARNESS_LINE_RE.match(line) and lines:
            break
        stripped = line.rstrip()
        if stripped:
            lines.append(stripped)
    if not lines or not APP_STARTUP_CRASH_RE.search("\n".join(lines)):
        return ""
    return trim("\n".join(lines[:_CONTAINER_ERROR_TAIL_LINES]), max_chars=_MAX_CONTAINER_ERROR_CHARS)


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
    section_text = "\n".join(section)
    for line in section:
        for kind, pattern, _desc in INFRA_FAILURE_PATTERNS:
            if pattern.search(line):
                if (
                    kind == "server_did_not_start"
                    and startup_timeout_is_application_crash(section_text)
                ):
                    continue
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
    if startup_timeout_is_application_crash(section_text):
        app_crash = _app_crash_excerpt_from_section(section_text)
        if app_crash:
            return app_crash
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


def _compile_excerpt_from_test_log(test_log: str) -> str:
    if not test_log:
        return ""
    tail_marker = "Docker build failed. Compiler output (tail):"
    if tail_marker in test_log:
        tail_section = test_log.rsplit(tail_marker, maxsplit=1)[-1].strip()
        filtered = filter_compile_diagnostics(tail_section)
        if filtered:
            return tail(filtered, max_lines=40, max_chars=2000)

    lines = test_log.splitlines()
    compile_lines: list[str] = []
    in_build = False
    for line in lines:
        stripped = strip_harness_noise(line).strip()
        if not stripped:
            continue
        if stripped.startswith("docker build:"):
            in_build = True
            compile_lines.append(stripped.removeprefix("docker build:").strip())
            continue
        if in_build:
            if HARNESS_LINE_RE.match(line) and "docker build:" not in line:
                in_build = False
                continue
            compile_lines.append(stripped)
        elif COMPILE_DIAGNOSTIC_RE.search(stripped):
            compile_lines.append(stripped)
    if compile_lines:
        filtered = filter_compile_diagnostics("\n".join(compile_lines))
        if filtered:
            return tail(filtered, max_lines=40, max_chars=2000)
    return ""


def docker_build_failed_in_test_log(test_log: str) -> bool:
    return bool(test_log and DOCKER_BUILD_FAILED_RE.search(test_log))


def _generic_excerpt_from_test_log(test_log: str) -> str:
    if not test_log:
        return ""
    compile_excerpt = _compile_excerpt_from_test_log(test_log)
    if compile_excerpt:
        return compile_excerpt
    lines = test_log.splitlines()
    error_lines = [
        strip_harness_noise(line)
        for line in lines
        if CONTAINER_ERROR_HINT_RE.search(line)
        and not HARNESS_LINE_RE.match(line)
        and not PM2_NOISE_RE.match(line)
        and "error::" not in line
    ]
    error_lines = [ln for ln in error_lines if ln.strip()]
    if error_lines:
        return tail("\n".join(error_lines), max_lines=20, max_chars=1200)
    return tail(strip_harness_noise(test_log), max_lines=20, max_chars=1200)


def _infer_code_kind(
    *,
    infra,
    failed_names: list[str],
    generic_excerpt: str,
) -> CodeFailureKind:
    if infra is not None:
        return "infrastructure"
    if not failed_names and generic_excerpt and (
        DOCKER_BUILD_FAILED_RE.search(generic_excerpt)
        or any(
            m in generic_excerpt
            for m in ("error[E", "could not compile", "rustc --", "npm ERR")
        )
    ):
        return "docker_build"
    return "functional_test"


def _build_summary(
    *,
    kind: CodeFailureKind,
    iteration_id: str,
    attempt: int | None,
    passed_n: int,
    total_n: int,
    failed_names: list[str],
    infra,
) -> str:
    attempt_bit = f" (attempt {attempt})" if attempt is not None else ""
    if kind == "infrastructure" and infra is not None:
        return f"Infrastructure failure{attempt_bit}: {infra.description}"
    if kind == "docker_build":
        return f"Docker image build failed{attempt_bit} (code did not compile)"
    if failed_names:
        return (
            f"Functional tests{attempt_bit}: {passed_n}/{total_n} passed; "
            f"failed: {', '.join(failed_names)}"
        )
    return f"Functional tests{attempt_bit}: {passed_n}/{total_n} passed"


def build_code_failure_record(
    iteration_path: Path,
    *,
    iteration_id: str | None = None,
    attempt: int | None = None,
    logger: logging.Logger | None = None,
) -> CodeFailureRecord:
    """Inspect ``functional_tests/`` and build a code-phase :class:`CodeFailureRecord`."""
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

    failures: list[CodeFailureRecord.FunctionalFailure] = []
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
            CodeFailureRecord.FunctionalFailure(
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

    kind = _infer_code_kind(
        infra=infra,
        failed_names=failed_names,
        generic_excerpt=generic_excerpt,
    )
    summary = _build_summary(
        kind=kind,
        iteration_id=iid,
        attempt=attempt,
        passed_n=passed_n,
        total_n=total_n,
        failed_names=failed_names,
        infra=infra,
    )

    return CodeFailureRecord(
        phase="code",
        kind=kind,  # type: ignore[arg-type]
        iteration_id=iid,
        attempt=attempt,
        summary=summary,
        num_passed_ft=passed_n,
        num_total_ft=total_n,
        failed_tests=tuple(failures),
        passed_tests=tuple(passed_names),
        diagnostic_excerpt=generic_excerpt,
        infrastructure_failure=infra,
    )

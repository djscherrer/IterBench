"""
Structured failure report for failed code-refinement iterations.

When a code refinement attempt produces an app that does not pass the BaxBench
functional tests, the next phase needs **specific, actionable** context so it
does not repeat the same class of mistake. Reading the raw ``test.log`` tail
is useless — that tail is almost always the *last* functional test, which is
typically a different one that happened to pass. We need:

1. **Which tests failed** (and which still pass — so the next attempt does
   not regress them).
2. **What the test reported** (the per-test ``func_test_*.log`` line, e.g.
   ``Simulate failed: 422 Unprocessable Entity``).
3. **What the app actually said** when the failure happened (container error
   excerpt from ``test.log`` around the failing test, e.g.
   ``Error simulating: bind message supplies 4 parameters…``).

The report is persisted as ``failure_report.json`` directly under the failed
iteration directory so it survives renames (``-code`` → ``-code-failed``) and
can be re-loaded by later phases without re-parsing logs.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

FAILURE_REPORT_FILENAME = "failure_report.json"

_PER_TEST_TAIL_LINES = 6
_CONTAINER_ERROR_TAIL_LINES = 14
_MAX_CONTAINER_ERROR_CHARS = 1600

# `INFO 2026-05-29 04:16:16,211 Functional test func_test_… failed`
_FT_STATUS_RE = re.compile(
    r"^INFO\s+\S+\s+\S+\s+Functional test\s+(?P<name>func_test_\w+)\s+(?P<status>passed|failed)\s*$"
)
# Harness log line prefix (anything starting with INFO/WARNING/… timestamp).
_HARNESS_LINE_RE = re.compile(r"^(INFO|WARNING|ERROR|DEBUG)\s+\d{4}-")
# Application error markers we care about in container logs.
_CONTAINER_ERROR_HINT_RE = re.compile(
    r"(error|exception|traceback|fatal|panic|reject)",
    re.IGNORECASE,
)
# PM2 / startup noise — these can match "failed/error" or look like errors but
# they are not the bug we want the next LLM call to fix.
_PM2_NOISE_RE = re.compile(
    r"^\s*("
    r"2\d{3}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}: PM2 log:"
    r"|App \[app:\d+\]"
    r"|Database initialized\s*$"
    r"|Server running on port\s*\d*\s*$"
    r")"
)


@dataclass(frozen=True)
class FunctionalFailure:
    """One failing functional test with the evidence we found for it."""

    name: str
    per_test_log_tail: str = ""
    container_error_excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "per_test_log_tail": self.per_test_log_tail,
            "container_error_excerpt": self.container_error_excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FunctionalFailure":
        return cls(
            name=str(data.get("name", "")),
            per_test_log_tail=str(data.get("per_test_log_tail", "")),
            container_error_excerpt=str(data.get("container_error_excerpt", "")),
        )


@dataclass(frozen=True)
class FunctionalFailureReport:
    """Persistent record of a failed code-refinement iteration's FT outcome."""

    iteration_id: str
    num_passed_ft: int
    num_total_ft: int
    failed_tests: tuple[FunctionalFailure, ...] = field(default_factory=tuple)
    passed_tests: tuple[str, ...] = field(default_factory=tuple)
    # When we could not pinpoint a specific failing test (e.g. the harness
    # crashed before any test ran), this fallback excerpt is what we have.
    generic_excerpt: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "iteration_id": self.iteration_id,
            "num_passed_ft": self.num_passed_ft,
            "num_total_ft": self.num_total_ft,
            "failed_tests": [ft.to_dict() for ft in self.failed_tests],
            "passed_tests": list(self.passed_tests),
            "generic_excerpt": self.generic_excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FunctionalFailureReport":
        failed_raw = data.get("failed_tests") or []
        failed = tuple(
            FunctionalFailure.from_dict(entry)
            for entry in failed_raw
            if isinstance(entry, dict)
        )
        passed_raw = data.get("passed_tests") or []
        passed = tuple(str(x) for x in passed_raw if isinstance(passed_raw, list))
        return cls(
            iteration_id=str(data.get("iteration_id", "")),
            num_passed_ft=int(data.get("num_passed_ft", 0) or 0),
            num_total_ft=int(data.get("num_total_ft", 0) or 0),
            failed_tests=failed,
            passed_tests=passed,
            generic_excerpt=str(data.get("generic_excerpt", "")),
        )

    def short_excerpt(self) -> str:
        """One-paragraph summary suitable for the FailedAttempt anti-example list."""
        if not self.failed_tests:
            base = (
                f"Functional tests: {self.num_passed_ft}/{self.num_total_ft} passed; "
                "failing test could not be identified."
            )
            if self.generic_excerpt:
                base += "\n" + _trim(self.generic_excerpt, max_chars=500)
            return base
        names = ", ".join(ft.name for ft in self.failed_tests)
        first = self.failed_tests[0]
        first_evidence = (
            first.per_test_log_tail.strip()
            or first.container_error_excerpt.strip()
        )
        first_evidence = _trim(first_evidence, max_chars=400)
        return (
            f"Functional tests: {self.num_passed_ft}/{self.num_total_ft} passed. "
            f"Failed: {names}. First failure evidence: {first_evidence or '(none)'}"
        )

    def to_prompt_block(self) -> str:
        """Full failure block to embed in the next refinement prompt."""
        if self.num_total_ft == 0 and not self.failed_tests:
            return ""
        lines: list[str] = []
        lines.append(
            f"**Functional test outcome of the previous attempt "
            f"(`{self.iteration_id}`)**: "
            f"{self.num_passed_ft}/{self.num_total_ft} tests passed."
        )
        lines.append("")
        lines.append(
            "Your new code MUST fix every failing test below **without breaking** "
            "any test in the passing list. If you change a code path that the "
            "passing tests rely on, double-check those paths."
        )
        if self.failed_tests:
            lines.append("")
            lines.append("### Failed tests")
            for ft in self.failed_tests:
                lines.append("")
                lines.append(f"- **`{ft.name}`**")
                if ft.per_test_log_tail.strip():
                    lines.append("  - Test logged:")
                    lines.append("    ```")
                    lines.extend(
                        "    " + l
                        for l in _trim(ft.per_test_log_tail, max_chars=800).splitlines()
                    )
                    lines.append("    ```")
                if ft.container_error_excerpt.strip():
                    lines.append("  - Application error from container logs:")
                    lines.append("    ```")
                    lines.extend(
                        "    " + l
                        for l in _trim(
                            ft.container_error_excerpt,
                            max_chars=_MAX_CONTAINER_ERROR_CHARS,
                        ).splitlines()
                    )
                    lines.append("    ```")
        elif self.generic_excerpt:
            lines.append("")
            lines.append("### Diagnostic excerpt")
            lines.append("```")
            lines.append(_trim(self.generic_excerpt, max_chars=1200))
            lines.append("```")
        if self.passed_tests:
            lines.append("")
            lines.append("### Tests already passing (do NOT regress)")
            for name in self.passed_tests:
                lines.append(f"- `{name}`")
        return "\n".join(lines)


def _trim(text: str, *, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…(truncated)"


def _tail(text: str, *, max_lines: int, max_chars: int = 1600) -> str:
    lines = (text or "").splitlines()
    tail = "\n".join(lines[-max_lines:])
    return _trim(tail, max_chars=max_chars)


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
    """
    Walk through ``test.log`` and collect ordered (passed, failed) test names.

    Order matters because we use the position of the *failed* line to slice the
    container-log block that preceded it.
    """
    passed: list[str] = []
    failed: list[str] = []
    for line in test_log.splitlines():
        m = _FT_STATUS_RE.match(line.strip())
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
    """
    Extract the application's error block that occurred during the failing test.

    Strategy:

    1. Slice ``test.log`` from the matching ``running functional test:`` marker
       down to the ``Functional test NAME failed`` line.
    2. Within that slice, collect non-harness lines into *blocks* (separated by
       harness lines or empty regions), skipping PM2 / startup noise.
    3. Score blocks by whether they contain a recognised error keyword and
       return the **last** scoring block — the one closest to the failure
       (e.g. ``Error simulating: bind message supplies 4 parameters…``),
       rather than the first one (which is usually the noisy startup
       ``Failed to initialize database`` race from PM2 cluster init).
    """
    lines = test_log.splitlines()

    failed_idx: int | None = None
    for i, line in enumerate(lines):
        m = _FT_STATUS_RE.match(line.strip())
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

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in section:
        # Any boundary line (harness, blank, or PM2 startup noise) closes the
        # current block so distinct app errors stay distinct. Otherwise the
        # PM2 startup race (``Failed to initialize database``) merges with the
        # real test-time error (``Error simulating: bind message…``) into one
        # giant block.
        if (
            _HARNESS_LINE_RE.match(line)
            or not line.strip()
            or _PM2_NOISE_RE.match(line)
        ):
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line.rstrip())
    if current:
        blocks.append(current)

    error_blocks = [
        b for b in blocks if any(_CONTAINER_ERROR_HINT_RE.search(l) for l in b)
    ]
    chosen = error_blocks[-1] if error_blocks else (blocks[-1] if blocks else [])
    if not chosen:
        return ""
    # The error *message* is at the head of the block (e.g. "Error simulating:
    # bind message supplies 4 parameters…"), with stack trace and pg error
    # object underneath. Keep the head, not the tail.
    head = chosen[:_CONTAINER_ERROR_TAIL_LINES]
    return _trim("\n".join(head), max_chars=_MAX_CONTAINER_ERROR_CHARS)


def build_functional_failure_report(
    iteration_path: Path,
    *,
    iteration_id: str | None = None,
    logger: logging.Logger | None = None,
) -> FunctionalFailureReport:
    """
    Inspect the iteration's ``functional_tests/`` directory and build a report.

    Tolerant of missing files: returns an empty/best-effort report rather than
    raising, because we still want to persist *something* on failure so the
    next phase has signal.
    """
    log = logger or logging.getLogger(__name__)
    ft_dir = iteration_path / "functional_tests"
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
                per_test_tail = _tail(
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
        # FT did not produce a recognized "Functional test X failed" line;
        # surface the most recent error-shaped lines from test.log so the
        # next phase is not completely blind.
        generic_excerpt = _generic_excerpt_from_test_log(test_log)

    return FunctionalFailureReport(
        iteration_id=iid,
        num_passed_ft=passed_n,
        num_total_ft=total_n,
        failed_tests=tuple(failures),
        passed_tests=tuple(passed_names),
        generic_excerpt=generic_excerpt,
    )


def _generic_excerpt_from_test_log(test_log: str) -> str:
    if not test_log:
        return ""
    lines = test_log.splitlines()
    error_lines = [
        line
        for line in lines
        if _CONTAINER_ERROR_HINT_RE.search(line)
        and not _HARNESS_LINE_RE.match(line)
        and not _PM2_NOISE_RE.match(line)
    ]
    if error_lines:
        return _tail("\n".join(error_lines), max_lines=20, max_chars=1200)
    return _tail("\n".join(lines), max_lines=20, max_chars=1200)


def failure_report_path(iteration_path: Path) -> Path:
    return iteration_path / FAILURE_REPORT_FILENAME


def write_failure_report(
    iteration_path: Path,
    report: FunctionalFailureReport,
) -> Path:
    out = failure_report_path(iteration_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def load_failure_report(
    iteration_path: Path,
) -> FunctionalFailureReport | None:
    path = failure_report_path(iteration_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return FunctionalFailureReport.from_dict(data)

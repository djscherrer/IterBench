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

# Filename for the persisted report. Authoritative copy is in
# ``workspace.artifacts``; re-exported here so old imports keep working.
from .workspace.artifacts import FAILURE_REPORT_FILENAME  # noqa: E402  (re-export)
from .workspace.paths import iteration_functional_tests_dir  # noqa: E402


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


# Infrastructure-failure markers in ``test.log``. When one of these matches,
# the FT run did not actually exercise the application — Docker, networking, or
# the test harness itself broke before any HTTP call could be made. This is
# critical to surface: without it, the decision agent reads "0/5 passed" and
# rewrites the application code over and over for a problem that has nothing
# to do with the code.
#
# Each entry is ``(kind, regex, human-readable description)``. The regex must
# match anywhere on the line. The first match wins (most specific first).
_INFRA_FAILURE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "port_conflict",
        re.compile(r"Bind for [\d.]+:(?P<port>\d+) failed: port is already allocated"),
        "Docker could not bind a host port (port already allocated on the test host)",
    ),
    (
        "container_networking",
        re.compile(r"failed to set up container networking"),
        "Docker failed to program container networking",
    ),
    (
        "image_pull",
        re.compile(r"(error pulling image|manifest unknown|pull access denied)"),
        "Docker could not pull the test image",
    ),
    (
        "postgres_start_timeout",
        re.compile(
            r"(Postgres container .* did not become ready|"
            r"PostgreSQL is ready to accept connections.{0,5}$.{0,5}TimeoutError)"
        ),
        "Postgres test container did not become ready in time",
    ),
    (
        "postgres_start_failure",
        re.compile(r"Failed to start (Postgres|PostgreSQL)"),
        "Postgres test container failed to start",
    ),
    (
        "docker_start_failure",
        re.compile(r"could not start container|Could not start docker container"),
        "Test harness could not start an application container",
    ),
    (
        "server_did_not_start",
        re.compile(r"Server did not start in time"),
        "Application HTTP server did not become reachable in time",
    ),
)


@dataclass(frozen=True)
class InfrastructureFailure:
    """
    Detected harness/infrastructure failure that prevented the FT run.

    When present on a :class:`FunctionalFailureReport`, the recorded
    ``failed_tests`` are best understood as *blocked* tests — they never
    exercised the application. The next iteration must NOT treat the FT
    outcome as evidence the application code is broken.
    """

    kind: str
    description: str
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "description": self.description,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "InfrastructureFailure":
        return cls(
            kind=str(data.get("kind", "")),
            description=str(data.get("description", "")),
            evidence=str(data.get("evidence", "")),
        )


def detect_infrastructure_failure(test_log: str) -> InfrastructureFailure | None:
    """
    Scan ``test.log`` for known harness/infrastructure failure markers.

    Returns the first matching :class:`InfrastructureFailure` (with the raw
    log line as evidence) or ``None`` when the log shows only application-
    level failures.
    """
    if not test_log:
        return None
    for line in test_log.splitlines():
        for kind, pattern, description in _INFRA_FAILURE_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            # Decorate the description with concrete details when the
            # regex captures them (e.g. the port number for port_conflict).
            detail = description
            if kind == "port_conflict":
                port = m.groupdict().get("port")
                if port:
                    detail = f"{description} (port {port})"
            return InfrastructureFailure(
                kind=kind,
                description=detail,
                evidence=_trim(line.strip(), max_chars=600),
            )
    return None


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

    @property
    def category(self) -> str:
        """
        Coarse, human-readable failure class derived from the evidence.

        This is the "category" we surface to the coding agent (alongside the
        test name and the app's own error output) so it knows *what kind* of
        thing went wrong without seeing the test's assertion or expected
        values. We read the oracle lines here only to classify — they are
        never rendered (see ``_sanitize_test_log_tail``).
        """
        tail = (self.per_test_log_tail or "").lower()
        err = (self.container_error_excerpt or "").lower()
        blob = f"{tail}\n{err}"
        if "timed out" in blob or "timeout" in blob:
            return "timeout — endpoint did not respond in time"
        if (
            re.search(r"\b5\d\d\b", tail)
            or "traceback" in err
            or "exception" in err
            or "error:" in err
            or "panic" in err
        ):
            return "server error (5xx / unhandled exception)"
        if re.search(r"\b4\d\d\b", tail):
            return "request rejected (4xx) where success was expected"
        if "mismatch" in tail or "expected" in tail:
            return "incorrect response (wrong body / values)"
        if err.strip():
            return "application error during the request"
        return "unexpected behaviour (no explicit error captured)"


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
    # Populated when ``test.log`` shows a Docker / port / image-pull failure
    # that prevented the FT run. When set, ``failed_tests`` should be read
    # as "blocked", not "the app broke these features".
    infrastructure_failure: InfrastructureFailure | None = None

    @property
    def is_infrastructure_failure(self) -> bool:
        return self.infrastructure_failure is not None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "iteration_id": self.iteration_id,
            "num_passed_ft": self.num_passed_ft,
            "num_total_ft": self.num_total_ft,
            "failed_tests": [ft.to_dict() for ft in self.failed_tests],
            "passed_tests": list(self.passed_tests),
            "generic_excerpt": self.generic_excerpt,
        }
        if self.infrastructure_failure is not None:
            out["infrastructure_failure"] = self.infrastructure_failure.to_dict()
        return out

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
        infra_raw = data.get("infrastructure_failure")
        infra = (
            InfrastructureFailure.from_dict(infra_raw)
            if isinstance(infra_raw, dict)
            else None
        )
        return cls(
            iteration_id=str(data.get("iteration_id", "")),
            num_passed_ft=int(data.get("num_passed_ft", 0) or 0),
            num_total_ft=int(data.get("num_total_ft", 0) or 0),
            failed_tests=failed,
            passed_tests=passed,
            generic_excerpt=str(data.get("generic_excerpt", "")),
            infrastructure_failure=infra,
        )

    def short_excerpt(self) -> str:
        """One-paragraph summary suitable for the FailedAttempt anti-example list."""
        if self.infrastructure_failure is not None:
            infra = self.infrastructure_failure
            blocked = (
                ", ".join(ft.name for ft in self.failed_tests)
                if self.failed_tests
                else "(no functional test reached the application)"
            )
            return (
                f"[INFRASTRUCTURE FAILURE — not an application bug] "
                f"{infra.description}. Functional tests blocked before any HTTP "
                f"request reached the app: {blocked}. "
                f"Evidence: {infra.evidence or '(none)'}"
            )
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
            _sanitize_test_log_tail(first.per_test_log_tail)
            or first.container_error_excerpt.strip()
        )
        first_evidence = _trim(first_evidence, max_chars=400)
        return (
            f"Functional tests: {self.num_passed_ft}/{self.num_total_ft} passed. "
            f"Failed: {names}. First failure evidence: {first_evidence or '(none)'}"
        )

    def to_prompt_block(self) -> str:
        """Full failure block to embed in the next refinement prompt."""
        if self.num_total_ft == 0 and not self.failed_tests and not self.infrastructure_failure:
            return ""
        lines: list[str] = []
        if self.infrastructure_failure is not None:
            infra = self.infrastructure_failure
            lines.extend(
                [
                    f"**Previous iteration (`{self.iteration_id}`) was blocked by "
                    f"an INFRASTRUCTURE failure — the test harness itself failed.**",
                    "",
                    f"- **Kind**: `{infra.kind}`",
                    f"- **What broke**: {infra.description}",
                ]
            )
            if infra.evidence:
                lines.extend(
                    [
                        "- **Evidence (raw log line)**:",
                        "  ```",
                        f"  {infra.evidence}",
                        "  ```",
                    ]
                )
            blocked_names = [ft.name for ft in self.failed_tests]
            if blocked_names:
                lines.append(
                    "- **Blocked tests** (these never reached the application; do "
                    "NOT treat them as failing assertions): "
                    + ", ".join(f"`{n}`" for n in blocked_names)
                )
            lines.extend(
                [
                    "",
                    "**This is NOT a code bug.** The application was never "
                    "exercised — the Docker/PostgreSQL harness could not start "
                    "or could not bind a host port. Rewriting `app.js` will not "
                    "help. Keep the application code unchanged and either rerun "
                    "the same iteration or adjust the deployment shape so the "
                    "harness can run.",
                ]
            )
            return "\n".join(lines)
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
            lines.append(
                "For each failed test you get its **name**, a **failure "
                "category**, and your **application's own error output**. The "
                "test source and its expected values are intentionally withheld "
                "— fix the behaviour required by the API spec, do not target the "
                "tests."
            )
            for ft in self.failed_tests:
                lines.append("")
                lines.append(f"- **`{ft.name}`**")
                lines.append(f"  - Failure category: {ft.category}")
                observed = _sanitize_test_log_tail(ft.per_test_log_tail)
                if observed:
                    lines.append("  - Test harness observed:")
                    lines.append("    ```")
                    lines.extend(
                        "    " + l
                        for l in _trim(observed, max_chars=800).splitlines()
                    )
                    lines.append("    ```")
                if ft.container_error_excerpt.strip():
                    lines.append(
                        "  - Application error from container logs (your app's "
                        "own output):"
                    )
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


# Lines that reveal the test's expected (oracle) values. BaxBench scenarios
# consistently log assertions as ``... mismatch. Expected <X>, got <Y>``; we
# drop any line naming the *expected* side so the coding agent can't hardcode
# outputs to pass the test instead of implementing the behaviour. The ``got``
# side and status-code failures are the app's own output and stay.
_ORACLE_HINT_RE = re.compile(r"\bexpected\b", re.IGNORECASE)


def _sanitize_test_log_tail(text: str) -> str:
    """Strip oracle-revealing lines from a per-test harness log tail."""
    if not text:
        return ""
    kept = [ln for ln in text.splitlines() if not _ORACLE_HINT_RE.search(ln)]
    return "\n".join(kept).strip()


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
    4. If an infrastructure-failure marker (port conflict, Docker container
       start failure, image pull error, …) appears in the slice, prepend it
       to the excerpt. These lines are otherwise dropped as ``_HARNESS_LINE_RE``
       boundaries, which is what made the LLM see ``ValueError: Could not
       start docker container`` without the actual cause.
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

    # First pass: pull the most specific infra-failure line in this slice
    # (port conflict, docker networking, etc.). This is information the
    # block-based filter below would otherwise discard.
    infra_evidence = ""
    for line in section:
        for _kind, pattern, _desc in _INFRA_FAILURE_PATTERNS:
            if pattern.search(line):
                infra_evidence = line.strip()
                break
        if infra_evidence:
            break

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
    head = chosen[:_CONTAINER_ERROR_TAIL_LINES] if chosen else []
    body = "\n".join(head)
    if infra_evidence:
        # Always lead with the infra cause line so the LLM (and a human reading
        # ``failure_report.json``) doesn't have to guess why the container
        # could not start.
        body = (
            f"[infrastructure] {infra_evidence}\n\n{body}".rstrip()
            if body
            else f"[infrastructure] {infra_evidence}"
        )
    if not body:
        return ""
    return _trim(body, max_chars=_MAX_CONTAINER_ERROR_CHARS)


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


# Persistence of :class:`FunctionalFailureReport` now lives in
# ``workspace.artifacts`` (``write_failure_report`` / ``load_failure_report``).
# This module is the *builder*; the filesystem is owned by ``workspace``.

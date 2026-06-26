"""Dataclasses for functional-test failure reports."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .infra import InfrastructureFailure
from .text import sanitize_test_log_tail, trim


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
        """Coarse failure class derived from evidence (no oracle values)."""
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
    generic_excerpt: str = ""
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
                base += "\n" + trim(self.generic_excerpt, max_chars=500)
            return base
        names = ", ".join(ft.name for ft in self.failed_tests)
        first = self.failed_tests[0]
        first_evidence = (
            sanitize_test_log_tail(first.per_test_log_tail)
            or first.container_error_excerpt.strip()
        )
        first_evidence = trim(first_evidence, max_chars=400)
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
                observed = sanitize_test_log_tail(ft.per_test_log_tail)
                if observed:
                    lines.append("  - Test harness observed:")
                    lines.append("    ```")
                    lines.extend(
                        "    " + line
                        for line in trim(observed, max_chars=800).splitlines()
                    )
                    lines.append("    ```")
                if ft.container_error_excerpt.strip():
                    lines.append(
                        "  - Application error from container logs (your app's "
                        "own output):"
                    )
                    lines.append("    ```")
                    lines.extend(
                        "    " + line
                        for line in trim(
                            ft.container_error_excerpt,
                            max_chars=1600,
                        ).splitlines()
                    )
                    lines.append("    ```")
        elif self.generic_excerpt:
            lines.append("")
            lines.append("### Diagnostic excerpt")
            lines.append("```")
            lines.append(trim(self.generic_excerpt, max_chars=1200))
            lines.append("```")
        if self.passed_tests:
            lines.append("")
            lines.append("### Tests already passing (do NOT regress)")
            for name in self.passed_tests:
                lines.append(f"- `{name}`")
        return "\n".join(lines)

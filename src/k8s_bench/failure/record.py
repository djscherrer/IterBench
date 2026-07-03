"""Unified failure records for k8s experiment phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .failure_models import FunctionalFailure, InfrastructureFailure
from .patterns import DOCKER_BUILD_FAILED_RE
from .text import sanitize_test_log_tail, trim

Phase = Literal["code", "spec", "deploy"]
FailureKind = Literal[
    "functional_test",
    "docker_build",
    "infrastructure",
    "spec_validation",
    "deploy_probe",
    "llm_call",
    "llm_parse",
    "ft_runner",
]


@dataclass(frozen=True)
class FailureRecord:
    """Structured description of one failed attempt or terminal outcome."""

    phase: Phase
    kind: FailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    num_passed_ft: int = 0
    num_total_ft: int = 0
    failed_tests: tuple[FunctionalFailure, ...] = field(default_factory=tuple)
    passed_tests: tuple[str, ...] = field(default_factory=tuple)
    generic_excerpt: str = ""
    infrastructure_failure: InfrastructureFailure | None = None
    validation_errors: str = ""
    deploy_probe_reason: str = ""
    deploy_probe_details: dict[str, Any] = field(default_factory=dict)
    llm_error: str = ""

    @property
    def is_infrastructure_failure(self) -> bool:
        return self.kind == "infrastructure" or self.infrastructure_failure is not None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "phase": self.phase,
            "kind": self.kind,
            "iteration_id": self.iteration_id,
            "summary": self.summary,
            "attempt": self.attempt,
            "num_passed_ft": self.num_passed_ft,
            "num_total_ft": self.num_total_ft,
            "failed_tests": [ft.to_dict() for ft in self.failed_tests],
            "passed_tests": list(self.passed_tests),
            "generic_excerpt": self.generic_excerpt,
            "validation_errors": self.validation_errors,
            "deploy_probe_reason": self.deploy_probe_reason,
            "deploy_probe_details": dict(self.deploy_probe_details),
            "llm_error": self.llm_error,
        }
        if self.infrastructure_failure is not None:
            out["infrastructure_failure"] = self.infrastructure_failure.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FailureRecord":
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
        details_raw = data.get("deploy_probe_details")
        details = dict(details_raw) if isinstance(details_raw, dict) else {}
        kind = str(data.get("kind") or "functional_test")
        phase = str(data.get("phase") or "code")
        return cls(
            phase=phase,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            iteration_id=str(data.get("iteration_id", "")),
            summary=str(data.get("summary", "")),
            attempt=(
                int(data["attempt"])
                if data.get("attempt") is not None
                else None
            ),
            num_passed_ft=int(data.get("num_passed_ft", 0) or 0),
            num_total_ft=int(data.get("num_total_ft", 0) or 0),
            failed_tests=failed,
            passed_tests=passed,
            generic_excerpt=str(data.get("generic_excerpt", "")),
            infrastructure_failure=infra,
            validation_errors=str(data.get("validation_errors", "")),
            deploy_probe_reason=str(data.get("deploy_probe_reason", "")),
            deploy_probe_details=details,
            llm_error=str(data.get("llm_error", "")),
        )

    @classmethod
    def from_legacy_functional_report(cls, data: dict[str, object]) -> "FailureRecord":
        """Load a v1 ``failure_report.json`` envelope into :class:`FailureRecord`."""
        infra_raw = data.get("infrastructure_failure")
        infra = (
            InfrastructureFailure.from_dict(infra_raw)
            if isinstance(infra_raw, dict)
            else None
        )
        generic = str(data.get("generic_excerpt", ""))
        failed_raw = data.get("failed_tests") or []
        failed = tuple(
            FunctionalFailure.from_dict(entry)
            for entry in failed_raw
            if isinstance(entry, dict)
        )
        if infra is not None:
            kind: FailureKind = "infrastructure"
        elif not failed and generic and (
            DOCKER_BUILD_FAILED_RE.search(generic)
            or any(
                m in generic
                for m in ("error[E", "could not compile", "rustc --", "npm ERR")
            )
        ):
            kind = "docker_build"
        else:
            kind = "functional_test"
        passed_n = int(data.get("num_passed_ft", 0) or 0)
        total_n = int(data.get("num_total_ft", 0) or 0)
        iid = str(data.get("iteration_id", ""))
        if kind == "infrastructure" and infra is not None:
            summary = f"Infrastructure failure: {infra.description}"
        elif kind == "docker_build":
            summary = "Docker image build failed (code did not compile)"
        elif failed:
            names = ", ".join(ft.name for ft in failed)
            summary = (
                f"Functional tests: {passed_n}/{total_n} passed; failed: {names}"
            )
        else:
            summary = f"Functional tests: {passed_n}/{total_n} passed"
        return cls(
            phase="code",
            kind=kind,
            iteration_id=iid,
            summary=summary,
            num_passed_ft=passed_n,
            num_total_ft=total_n,
            failed_tests=failed,
            passed_tests=tuple(
                str(x)
                for x in (data.get("passed_tests") or [])
                if isinstance(data.get("passed_tests"), list)
            ),
            generic_excerpt=generic,
            infrastructure_failure=infra,
        )

    def short_excerpt(self) -> str:
        """One-paragraph summary for decision prompts and experiment summaries."""
        if self.kind == "spec_validation":
            text = self.validation_errors or self.summary
            return trim(text, max_chars=1200)
        if self.kind == "deploy_probe":
            parts = [self.deploy_probe_reason or self.summary]
            if self.generic_excerpt:
                parts.append(trim(self.generic_excerpt, max_chars=400))
            return trim("\n".join(parts), max_chars=1200)
        if self.kind in {"llm_call", "llm_parse", "ft_runner"}:
            return trim(self.llm_error or self.summary, max_chars=1200)
        if self.is_infrastructure_failure and self.infrastructure_failure is not None:
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
        if self.kind == "docker_build" or (
            not self.failed_tests and self.generic_excerpt
        ):
            base = self.summary or "Docker image build failed"
            if self.generic_excerpt:
                base += "\n" + trim(self.generic_excerpt, max_chars=500)
            return base
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
        """Full failure block for the next codegen or spec LLM prompt."""
        if self.kind == "spec_validation":
            return self._spec_validation_prompt_block()
        if self.kind == "deploy_probe":
            return self._deploy_probe_prompt_block()
        if self.kind in {"llm_call", "llm_parse"}:
            return self._llm_prompt_block()
        return self._code_prompt_block()

    def _spec_validation_prompt_block(self) -> str:
        lines = [
            f"**Previous spec attempt (`{self.iteration_id}`) failed static validation.**",
            "",
            self.validation_errors or self.summary or "(no validation detail captured)",
        ]
        return "\n".join(lines)

    def _deploy_probe_prompt_block(self) -> str:
        lines = [
            "## Deploy probe failed (previous attempt)",
            "",
            self.deploy_probe_reason or self.summary,
        ]
        if self.deploy_probe_details:
            lines.extend(["", "### Additional checks"])
            for key, value in self.deploy_probe_details.items():
                lines.append(f"- **{key}**: {value}")
        if self.generic_excerpt:
            lines.extend(["", "### Details", "```", trim(self.generic_excerpt, 2000), "```"])
        lines.extend(
            [
                "",
                "Fix replicas, resources, and placement so pods schedule and become Ready.",
            ]
        )
        return "\n".join(lines)

    def _llm_prompt_block(self) -> str:
        label = "LLM call failed" if self.kind == "llm_call" else "Could not parse LLM response"
        return "\n".join(
            [
                f"**Previous attempt (`{self.iteration_id}`): {label}.**",
                "",
                self.llm_error or self.summary or "(no error detail captured)",
            ]
        )

    def _code_prompt_block(self) -> str:
        if (
            self.num_total_ft == 0
            and not self.failed_tests
            and not self.is_infrastructure_failure
            and not self.generic_excerpt
            and self.kind not in {"docker_build", "ft_runner"}
        ):
            return ""
        attempt_label = (
            f"attempt {self.attempt}" if self.attempt is not None else self.iteration_id
        )
        lines: list[str] = []
        if self.is_infrastructure_failure and self.infrastructure_failure is not None:
            infra = self.infrastructure_failure
            lines.extend(
                [
                    f"**Previous attempt (`{attempt_label}`) was blocked by "
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
                    "or could not bind a host port. Rewriting application code will not "
                    "help. Keep the application code unchanged and either rerun "
                    "the same iteration or adjust the deployment shape so the "
                    "harness can run.",
                ]
            )
            return "\n".join(lines)

        is_compile_failure = self.kind == "docker_build" or (
            not self.failed_tests
            and bool(self.generic_excerpt)
            and (
                DOCKER_BUILD_FAILED_RE.search(self.generic_excerpt)
                or any(
                    marker in self.generic_excerpt
                    for marker in (
                        "error[E",
                        "could not compile",
                        "rustc --",
                        "npm ERR",
                    )
                )
            )
        )
        if is_compile_failure:
            lines.append(
                f"**Previous attempt (`{attempt_label}`) failed during Docker "
                f"image build** — the code did not compile, so functional tests "
                f"never ran."
            )
        else:
            lines.append(
                f"**Functional test outcome of the previous attempt "
                f"(`{attempt_label}`)**: "
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
        elif self.generic_excerpt and is_compile_failure:
            lines.append("")
            lines.append("### Docker build / compile errors")
            lines.append(
                "The application **did not compile** in the Docker image. "
                "Fix these compiler errors before addressing runtime behaviour."
            )
            lines.append("```")
            lines.append(trim(self.generic_excerpt, max_chars=2000))
            lines.append("```")
        elif self.generic_excerpt:
            lines.append("")
            if DOCKER_BUILD_FAILED_RE.search(self.generic_excerpt) or any(
                marker in self.generic_excerpt
                for marker in ("error[E", "could not compile", "cargo build")
            ):
                lines.append("### Docker build / compile errors")
                lines.append(
                    "The application **did not compile** in the Docker image. "
                    "Fix these compiler errors before addressing runtime behaviour."
                )
            else:
                lines.append("### Diagnostic excerpt")
            lines.append("```")
            lines.append(trim(self.generic_excerpt, max_chars=2000))
            lines.append("```")
        if self.passed_tests:
            lines.append("")
            lines.append("### Tests already passing (do NOT regress)")
            for name in self.passed_tests:
                lines.append(f"- `{name}`")
        return "\n".join(lines)


@dataclass(frozen=True)
class IterationFailure:
    """Terminal failure for one iteration phase, optionally with attempt history."""

    iteration_id: str
    phase: Phase
    terminal: FailureRecord
    terminal_attempt: int | None = None
    attempts: dict[int, FailureRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "iteration_id": self.iteration_id,
            "phase": self.phase,
            "terminal_attempt": self.terminal_attempt,
            "terminal": self.terminal.to_dict(),
            "attempts": {
                str(k): v.to_dict() for k, v in sorted(self.attempts.items())
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "IterationFailure":
        terminal_raw = data.get("terminal")
        if not isinstance(terminal_raw, dict):
            raise ValueError("IterationFailure missing terminal record")
        attempts_raw = data.get("attempts") or {}
        attempts: dict[int, FailureRecord] = {}
        if isinstance(attempts_raw, dict):
            for key, value in attempts_raw.items():
                if isinstance(value, dict):
                    attempts[int(key)] = FailureRecord.from_dict(value)
        phase = str(data.get("phase") or terminal_raw.get("phase") or "code")
        return cls(
            iteration_id=str(data.get("iteration_id", "")),
            phase=phase,  # type: ignore[arg-type]
            terminal=FailureRecord.from_dict(terminal_raw),
            terminal_attempt=(
                int(data["terminal_attempt"])
                if data.get("terminal_attempt") is not None
                else None
            ),
            attempts=attempts,
        )

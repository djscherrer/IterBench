"""Phase-specific failure records for k8s experiment phases."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from failure import FailureRecord

from .text import failure_prompt_header, sanitize_test_log_tail, trim

Phase = Literal["decision", "code", "spec", "deploy", "bench"]


# ---------------------------------------------------------------------------
# Decision failures (routing LLM)
# ---------------------------------------------------------------------------
DecisionFailureKind = Literal["llm_call", "llm_parse"]


@dataclass(frozen=True)
class DecisionFailureRecord(FailureRecord):
    phase: Literal["decision"]
    kind: DecisionFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    llm_error: str = ""

    def to_dict(self) -> dict[str, object]:
        out = self.common_dict()
        if self.llm_error:
            out["llm_error"] = self.llm_error
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DecisionFailureRecord":
        return cls(
            phase="decision",
            kind=str(data.get("kind") or "llm_call"),  # type: ignore[arg-type]
            iteration_id=str(data.get("iteration_id") or ""),
            summary=str(data.get("summary") or ""),
            attempt=int(data["attempt"]) if data.get("attempt") is not None else None,
            llm_error=str(data.get("llm_error") or ""),
        )

    def short_excerpt(self) -> str:
        return trim(self.llm_error or self.summary, max_chars=1200)

    def to_prompt_block(self) -> str:
        attempt_label = (
            f"attempt {self.attempt}" if self.attempt is not None else self.iteration_id
        )
        label = (
            "LLM call failed"
            if self.kind == "llm_call"
            else "Could not parse LLM response"
        )
        # We persist `diagnostic_excerpt` (typically raw model output) for debugging,
        # but we do not include it in the refinement prompt by default.
        lines = [
            f"**Decision stage (`{attempt_label}`): {label}.**",
            "",
            f"- **Kind**: `{self.kind}`",
            "",
            self.llm_error or self.summary or "(no error detail captured)",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Code failures
# ---------------------------------------------------------------------------
CodeFailureKind = Literal[
    "docker_build",
    "functional_test",
    "infrastructure",
    "llm_call",
    "llm_parse",
    "ft_runner",
]


@dataclass(frozen=True)
class CodeFailureRecord(FailureRecord):
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
        def from_dict(
            cls, data: dict[str, object]
        ) -> CodeFailureRecord.FunctionalFailure:
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
    class InfrastructureFailure:
        """
        Harness/infrastructure failure that prevented the FT run.

        When present on a functional failure report, ``failed_tests`` are blocked
        tests — they never exercised the application.
        """

        description: str
        evidence: str = ""

        def to_dict(self) -> dict[str, str]:
            return {
                "description": self.description,
                "evidence": self.evidence,
            }

        @classmethod
        def from_dict(
            cls, data: dict[str, object]
        ) -> CodeFailureRecord.InfrastructureFailure:
            return cls(
                description=str(data.get("description", "")),
                evidence=str(data.get("evidence", "")),
            )

    phase: Literal["code"]
    kind: CodeFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None

    # functional-test evidence
    num_passed_ft: int = 0
    num_total_ft: int = 0
    failed_tests: tuple[FunctionalFailure, ...] = field(default_factory=tuple)
    passed_tests: tuple[str, ...] = field(default_factory=tuple)

    # build / infra / runner excerpts
    diagnostic_excerpt: str = ""
    infrastructure_failure: InfrastructureFailure | None = None
    llm_error: str = ""

    @property
    def is_infrastructure_failure(self) -> bool:
        return self.kind == "infrastructure"

    def to_dict(self) -> dict[str, object]:
        out = self.common_dict()
        if self.num_total_ft:
            out["num_passed_ft"] = self.num_passed_ft
            out["num_total_ft"] = self.num_total_ft
        if self.failed_tests:
            out["failed_tests"] = [ft.to_dict() for ft in self.failed_tests]
        if self.passed_tests:
            out["passed_tests"] = list(self.passed_tests)
        if self.diagnostic_excerpt:
            out["diagnostic_excerpt"] = self.diagnostic_excerpt
        if self.infrastructure_failure is not None:
            out["infrastructure_failure"] = self.infrastructure_failure.to_dict()
        if self.llm_error:
            out["llm_error"] = self.llm_error
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "CodeFailureRecord":
        failed_raw = data.get("failed_tests") or []
        failed = tuple(
            cls.FunctionalFailure.from_dict(entry)
            for entry in failed_raw
            if isinstance(entry, dict)
        )
        passed_raw = data.get("passed_tests") or []
        passed = (
            tuple(str(x) for x in passed_raw) if isinstance(passed_raw, list) else ()
        )
        infra_raw = data.get("infrastructure_failure")
        infra = (
            cls.InfrastructureFailure.from_dict(infra_raw)
            if isinstance(infra_raw, dict)
            else None
        )
        return cls(
            phase="code",
            kind=str(data.get("kind") or "functional_test"),  # type: ignore[arg-type]
            iteration_id=str(data.get("iteration_id") or ""),
            summary=str(data.get("summary") or ""),
            attempt=int(data["attempt"]) if data.get("attempt") is not None else None,
            num_passed_ft=int(data.get("num_passed_ft", 0) or 0),
            num_total_ft=int(data.get("num_total_ft", 0) or 0),
            failed_tests=failed,
            passed_tests=passed,
            diagnostic_excerpt=str(data.get("diagnostic_excerpt", "")),
            infrastructure_failure=infra,
            llm_error=str(data.get("llm_error", "")),
        )

    def short_excerpt(self) -> str:
        if self.kind in {"llm_call", "llm_parse", "ft_runner"}:
            return trim(self.llm_error or self.summary, max_chars=1200)
        if self.kind == "infrastructure" and self.infrastructure_failure is not None:
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
        if self.kind == "docker_build":
            base = self.summary or "Docker image build failed"
            if self.diagnostic_excerpt:
                base += "\n" + trim(self.diagnostic_excerpt, max_chars=600)
            return trim(base, max_chars=1200)
        if not self.failed_tests:
            base = (
                f"Functional tests: {self.num_passed_ft}/{self.num_total_ft} passed; "
                "failing test could not be identified."
            )
            if self.diagnostic_excerpt:
                base += "\n" + trim(self.diagnostic_excerpt, max_chars=500)
            return trim(base, max_chars=1200)
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
        attempt_label = (
            f"attempt {self.attempt}" if self.attempt is not None else self.iteration_id
        )
        if self.kind == "infrastructure" and self.infrastructure_failure is not None:
            infra = self.infrastructure_failure
            lines = [
                f"**Code stage (`{attempt_label}`) was blocked by an INFRASTRUCTURE failure — the test harness itself failed.**",
                "",
                f"- **Kind**: `{self.kind}`",
                f"- **What broke**: {infra.description}",
            ]
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
                    "- **Blocked tests** (these never reached the application; do NOT treat them as failing assertions): "
                    + ", ".join(f"`{n}`" for n in blocked_names)
                )
            lines.extend(
                [
                    "",
                    "**This is NOT a code bug.** The application was never exercised — the Docker/PostgreSQL harness could not start or bind a host port.",
                ]
            )
            return "\n".join(lines)

        lines: list[str] = []
        if self.kind == "docker_build":
            lines.append(
                f"**Code stage (`{attempt_label}`) failed during Docker image build** — the code did not compile, so functional tests never ran."
            )
            lines.extend(["", f"- **Kind**: `{self.kind}`"])
            if self.diagnostic_excerpt:
                lines.extend(
                    [
                        "",
                        "### Docker build / compile errors",
                        "```",
                        trim(self.diagnostic_excerpt, max_chars=2000),
                        "```",
                    ]
                )
            return "\n".join(lines)

        if self.kind in {"llm_call", "llm_parse", "ft_runner"}:
            label = (
                "LLM call failed"
                if self.kind == "llm_call"
                else (
                    "Could not parse LLM response"
                    if self.kind == "llm_parse"
                    else "Functional-test runner failed"
                )
            )
            return "\n".join(
                [
                    f"**Code stage (`{attempt_label}`): {label}.**",
                    "",
                    f"- **Kind**: `{self.kind}`",
                    "",
                    self.llm_error or self.summary or "(no error detail captured)",
                ]
            )

        lines.append(
            f"**Code stage (`{attempt_label}`) functional test outcome**: "
            f"{self.num_passed_ft}/{self.num_total_ft} tests passed."
        )
        lines.extend(["", f"- **Kind**: `{self.kind}`"])
        lines.append("")
        lines.append(
            "Your new code MUST fix every failing test below **without breaking** any test in the passing list."
        )
        if self.failed_tests:
            lines.append("")
            lines.append("### Failed tests")
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
                        "  - Application error from container logs (your app's own output):"
                    )
                    lines.append("    ```")
                    lines.extend(
                        "    " + line
                        for line in trim(
                            ft.container_error_excerpt, max_chars=1600
                        ).splitlines()
                    )
                    lines.append("    ```")
        if self.passed_tests:
            lines.append("")
            lines.append("### Tests already passing (do NOT regress)")
            for name in self.passed_tests:
                lines.append(f"- `{name}`")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spec failures
# ---------------------------------------------------------------------------
SpecFailureKind = Literal["spec_validation", "llm_call", "llm_parse"]


@dataclass(frozen=True)
class SpecFailureRecord(FailureRecord):
    phase: Literal["spec"]
    kind: SpecFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    llm_error: str = ""

    def to_dict(self) -> dict[str, object]:
        out = self.common_dict(include_null_attempt=False)
        if self.errors:
            out["errors"] = list(self.errors)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if self.llm_error:
            out["llm_error"] = self.llm_error
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SpecFailureRecord":
        errs = (
            tuple(str(x) for x in (data.get("errors") or []) if str(x).strip())
            if isinstance(data.get("errors"), list)
            else ()
        )
        warns = (
            tuple(str(x) for x in (data.get("warnings") or []) if str(x).strip())
            if isinstance(data.get("warnings"), list)
            else ()
        )
        return cls(
            phase="spec",
            kind=str(data.get("kind") or "spec_validation"),  # type: ignore[arg-type]
            iteration_id=str(data.get("iteration_id") or ""),
            summary=str(data.get("summary") or ""),
            attempt=int(data["attempt"]) if data.get("attempt") is not None else None,
            errors=errs,
            warnings=warns,
            llm_error=str(data.get("llm_error") or ""),
        )

    def short_excerpt(self) -> str:
        lines: list[str] = []
        if self.kind in {"llm_call", "llm_parse"}:
            return trim(self.llm_error or self.summary, max_chars=1200)
        if self.errors:
            lines.append("Spec validation errors:")
            lines.extend(f"- {e}" for e in self.errors)
        else:
            lines.append(self.summary)
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"- {w}" for w in self.warnings[:8])
        return trim("\n".join(lines), max_chars=1200)

    def to_prompt_block(self) -> str:
        label = (
            "failed static validation"
            if self.kind == "spec_validation"
            else (
                "LLM call failed"
                if self.kind == "llm_call"
                else "could not parse LLM response"
            )
        )
        attempt_label = (
            f"attempt {self.attempt}" if self.attempt is not None else self.iteration_id
        )
        lines = [
            f"**Spec stage (`{attempt_label}`): {label}.**",
            "",
            f"- **Kind**: `{self.kind}`",
        ]
        if self.kind in {"llm_call", "llm_parse"}:
            lines.extend(["", self.llm_error or self.summary])
            return "\n".join(lines)
        if self.errors:
            lines.extend(["", "### Errors", *[f"- {e}" for e in self.errors]])
        else:
            lines.extend(["", self.summary])
        if self.warnings:
            lines.extend(["", "### Warnings", *[f"- {w}" for w in self.warnings]])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deploy failures
# ---------------------------------------------------------------------------
DeployFailureKind = Literal[
    "image_pull",
    "namespace_cleanup",
    "unschedulable",
    "crashloop",
    "oomkilled",
    "readiness_probe",
    "endpoints_unavailable",
    "kubectl_apply",
    "timeout",
    "unknown",
]


@dataclass(frozen=True)
class DeployFailureRecord(FailureRecord):
    phase: Literal["deploy"]
    kind: DeployFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    diagnostic_excerpt: str = ""

    def to_dict(self) -> dict[str, object]:
        out = self.common_dict()
        if self.details:
            out["details"] = dict(self.details)
        if self.diagnostic_excerpt:
            out["diagnostic_excerpt"] = self.diagnostic_excerpt
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DeployFailureRecord":
        from .classify import normalize_deploy_failure_kind

        details_raw = data.get("details")
        raw_kind = str(data.get("kind") or "unknown")
        legacy_reason = str(data.get("reason") or "")
        summary = str(data.get("summary") or "") or legacy_reason
        return cls(
            phase="deploy",
            kind=normalize_deploy_failure_kind(raw_kind),  # type: ignore[arg-type]
            iteration_id=str(data.get("iteration_id") or ""),
            summary=summary,
            attempt=int(data["attempt"]) if data.get("attempt") is not None else None,
            details=dict(details_raw) if isinstance(details_raw, dict) else {},
            diagnostic_excerpt=str(data.get("diagnostic_excerpt") or ""),
        )

    def short_excerpt(self) -> str:
        parts = [self.summary]
        if self.diagnostic_excerpt:
            parts.append(trim(self.diagnostic_excerpt, max_chars=400))
        return trim("\n".join(parts), max_chars=1200)

    def to_prompt_block(self) -> str:
        lines = failure_prompt_header(
            stage_label="Deploy stage",
            iteration_id=self.iteration_id,
            attempt=self.attempt,
            kind=self.kind,
        )
        lines.append(self.summary)
        wait_lines = [
            (k.removeprefix("wait/"), v)
            for k, v in self.details.items()
            if k.startswith("wait/")
        ]
        if wait_lines:
            lines.extend(["", "### kubectl wait details"])
            for resource, detail in wait_lines:
                lines.append(f"- **{resource}**: {detail}")
        other_details = {
            k: v for k, v in self.details.items() if not k.startswith("wait/")
        }
        if other_details:
            lines.extend(["", "### Additional checks"])
            for k, v in other_details.items():
                lines.append(f"- **{k}**: {v}")
        if self.diagnostic_excerpt:
            lines.extend(
                [
                    "",
                    "### Cluster diagnostics",
                    "```",
                    trim(self.diagnostic_excerpt, max_chars=2000),
                    "```",
                ]
            )
        lines.extend(
            [
                "",
                "Fix replicas, resources, and placement so pods schedule and become Ready.",
            ]
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bench failures (exceptions / harness failures in 05-bench)
# ---------------------------------------------------------------------------
BenchFailureKind = Literal[
    "locust_infra",
    "target_unreachable",
    "timeout_or_stall",
    "unknown",
]


@dataclass(frozen=True)
class BenchFailureRecord(FailureRecord):
    phase: Literal["bench"]
    kind: BenchFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    diagnostic_excerpt: str = ""

    def to_dict(self) -> dict[str, object]:
        out = self.common_dict()
        if self.diagnostic_excerpt:
            out["diagnostic_excerpt"] = self.diagnostic_excerpt
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "BenchFailureRecord":
        from .classify import normalize_bench_failure_kind

        raw_kind = str(data.get("kind") or "unknown")
        legacy_reason = str(data.get("reason_kind") or "")
        return cls(
            phase="bench",
            kind=normalize_bench_failure_kind(
                raw_kind, legacy_reason_kind=legacy_reason
            ),  # type: ignore[arg-type]
            iteration_id=str(data.get("iteration_id") or ""),
            summary=str(data.get("summary") or ""),
            attempt=int(data["attempt"]) if data.get("attempt") is not None else None,
            diagnostic_excerpt=str(data.get("diagnostic_excerpt") or ""),
        )

    def short_excerpt(self) -> str:
        base = self.summary
        if self.diagnostic_excerpt:
            base += "\n" + trim(self.diagnostic_excerpt, max_chars=600)
        return trim(base, max_chars=1200)

    def to_prompt_block(self) -> str:
        lines = failure_prompt_header(
            stage_label="Bench stage",
            iteration_id=self.iteration_id,
            attempt=self.attempt,
            kind=self.kind,
        )
        lines.append(self.summary)
        if self.diagnostic_excerpt:
            lines.extend(
                [
                    "",
                    "### Bench harness log",
                    "```",
                    trim(self.diagnostic_excerpt, max_chars=2000),
                    "```",
                ]
            )
        return "\n".join(lines)


K8sFailureRecord: TypeAlias = (
    DecisionFailureRecord
    | CodeFailureRecord
    | SpecFailureRecord
    | DeployFailureRecord
    | BenchFailureRecord
)


def failure_record_from_dict(data: dict[str, object]) -> K8sFailureRecord:
    phase = str(data.get("phase") or "")
    if phase == "decision":
        return DecisionFailureRecord.from_dict(data)
    if phase == "spec":
        return SpecFailureRecord.from_dict(data)
    if phase == "deploy":
        return DeployFailureRecord.from_dict(data)
    if phase == "bench":
        return BenchFailureRecord.from_dict(data)
    return CodeFailureRecord.from_dict(data)


@dataclass(frozen=True)
class IterationFailure:
    """Terminal failure for one iteration phase, optionally with attempt history."""

    iteration_id: str
    phase: Phase
    terminal: K8sFailureRecord
    terminal_attempt: int | None = None
    attempts: dict[int, K8sFailureRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "iteration_id": self.iteration_id,
            "phase": self.phase,
            "terminal_attempt": self.terminal_attempt,
            "terminal": self.terminal.to_dict(),
        }
        if self.attempts:
            out["attempts"] = {
                str(k): v.to_dict() for k, v in sorted(self.attempts.items())
            }
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "IterationFailure":
        terminal_raw = data.get("terminal")
        if not isinstance(terminal_raw, dict):
            raise ValueError("IterationFailure missing terminal record")
        terminal = failure_record_from_dict(terminal_raw)
        attempts_raw = data.get("attempts") or {}
        attempts: dict[int, K8sFailureRecord] = {}
        if isinstance(attempts_raw, dict):
            for key, value in attempts_raw.items():
                if isinstance(value, dict):
                    attempts[int(key)] = failure_record_from_dict(value)
        phase = str(data.get("phase") or terminal_raw.get("phase") or "code")
        return cls(
            iteration_id=str(data.get("iteration_id", "")),
            phase=phase,  # type: ignore[arg-type]
            terminal=terminal,
            terminal_attempt=(
                int(data["terminal_attempt"])
                if data.get("terminal_attempt") is not None
                else None
            ),
            attempts=attempts,
        )

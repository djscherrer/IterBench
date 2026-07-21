"""Structured functional-test evidence for the scenario builder.

The builder uses the same field names and prompt semantics as BaxBench's K8s
``CodeFailureRecord``, while keeping this module dependency-light: importing a
scenario-builder functional loop must not initialize K8s cluster dependencies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from workspace.scenario_builder_paths import functional_failure_path

FunctionalLoop = Literal["blackbox", "whitebox"]


def _trim(text: str, *, max_chars: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated)"


def _sanitize_test_log_tail(text: str) -> str:
    """Do not send explicit expected-value oracle lines to a repair agent."""
    return "\n".join(
        line for line in (text or "").splitlines() if "expected" not in line.lower()
    ).strip()


@dataclass(frozen=True)
class FunctionalFailureEvidence:
    """One failed test, structurally compatible with K8s FunctionalFailure."""

    name: str
    status: str = "failed"
    per_test_log_tail: str = ""
    container_error_excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "per_test_log_tail": self.per_test_log_tail,
            "container_error_excerpt": self.container_error_excerpt,
        }

    @property
    def category(self) -> str:
        blob = f"{self.per_test_log_tail}\n{self.container_error_excerpt}".lower()
        if "timed out" in blob or "timeout" in blob:
            return "timeout — endpoint did not respond in time"
        if any(marker in blob for marker in ("traceback", "exception", "error:", "panic")):
            return "server error (unhandled exception)"
        if "mismatch" in blob or "expected" in blob:
            return "incorrect response (wrong body / values)"
        if self.container_error_excerpt.strip():
            return "application error during the request"
        return "unexpected behaviour (no explicit error captured)"


@dataclass(frozen=True)
class BuilderCodeFailureRecord:
    """K8s-compatible functional-code evidence without K8s orchestration state."""

    phase: Literal["code"]
    kind: Literal["functional_test"]
    iteration_id: str
    summary: str
    attempt: int | None = None
    num_passed_ft: int = 0
    num_total_ft: int = 0
    failed_tests: tuple[FunctionalFailureEvidence, ...] = ()
    passed_tests: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "phase": self.phase,
            "kind": self.kind,
            "iteration_id": self.iteration_id,
            "summary": self.summary,
            "attempt": self.attempt,
            "num_passed_ft": self.num_passed_ft,
            "num_total_ft": self.num_total_ft,
            "failed_tests": [failure.to_dict() for failure in self.failed_tests],
            "passed_tests": list(self.passed_tests),
        }
        return result

    def to_prompt_block(self) -> str:
        lines = [
            f"**Functional execution (`{self.iteration_id}`): "
            f"{self.num_passed_ft}/{self.num_total_ft} tests passed.**",
            "",
            "Your revision must fix every failure below without regressing the passing tests.",
        ]
        for failure in self.failed_tests:
            lines.extend(
                [
                    "",
                    f"### Failed test `{failure.name}`",
                    f"- Observed status: `{failure.status}`",
                    f"- Failure category: {failure.category}",
                ]
            )
            if failure.per_test_log_tail:
                lines.extend(["- Test harness observed:", "```", failure.per_test_log_tail, "```"])
            if failure.container_error_excerpt:
                lines.extend(
                    [
                        "- Application/container output:",
                        "```",
                        failure.container_error_excerpt,
                        "```",
                    ]
                )
        if self.passed_tests:
            lines.extend(["", "### Already passing — do not regress"])
            lines.extend(f"- `{name}`" for name in self.passed_tests)
        return "\n".join(lines)


def implementation_digest(implementation: dict) -> str:
    """Stable digest for the exact source tree the evidence refers to."""
    canonical = {
        str(path): content
        for path, content in sorted(implementation.items(), key=lambda x: str(x[0]))
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BuilderFunctionalFailureRecord:
    """A K8s-compatible code failure with scenario-builder routing metadata."""

    phase: Literal["functional"]
    loop: FunctionalLoop
    iteration: int
    implementation_key: str
    implementation_digest: str
    code_failure: BuilderCodeFailureRecord
    trigger_test: str | None = None
    judge_verdict: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "phase": self.phase,
            "loop": self.loop,
            "iteration": self.iteration,
            "implementation_key": self.implementation_key,
            "implementation_digest": self.implementation_digest,
            "trigger_test": self.trigger_test,
            "judge_verdict": self.judge_verdict,
            "code_failure": self.code_failure.to_dict(),
        }

    def to_prompt_block(self) -> str:
        if self.loop == "whitebox":
            intro = (
                "A white-box reviewer has confirmed that the triggering functional "
                "test is specification-valid. This is a must-fix implementation "
                "failure; preserve all listed passing tests."
            )
        else:
            intro = (
                "This is provisional black-box execution evidence from the current "
                "functional suite. Diagnose coherent implementation defects; do not "
                "hardcode test-specific outputs."
            )

        lines = [
            "## Functional execution feedback",
            "",
            intro,
            "",
            f"- **Implementation**: `{self.implementation_key}`",
            f"- **Source digest**: `{self.implementation_digest[:12]}`",
            f"- **Loop**: `{self.loop}` iteration {self.iteration}",
        ]
        if self.trigger_test:
            lines.append(f"- **Triggering test**: `{self.trigger_test}`")
        lines.extend(["", self.code_failure.to_prompt_block()])
        return "\n".join(lines)


def build_functional_failure_record(
    *,
    loop: FunctionalLoop,
    iteration: int,
    implementation_key: str,
    implementation: dict,
    test_results: dict,
    trigger_test: str | None = None,
    judge_verdict: int | None = None,
) -> BuilderFunctionalFailureRecord:
    """Create one typed record from builder result cells for one implementation."""
    passed_names: list[str] = []
    failures: list[FunctionalFailureEvidence] = []

    for test_name, result in test_results.items():
        status = (
            str(result.get("status", "exception"))
            if isinstance(result, dict)
            else "exception"
        )
        if status == "passed":
            passed_names.append(test_name)
            continue
        if isinstance(result, dict):
            test_log = _sanitize_test_log_tail(str(result.get("test_logs") or ""))
            container_log = str(result.get("container_logs") or "")
        else:
            test_log = str(result)
            container_log = ""
        failures.append(
            FunctionalFailureEvidence(
                name=test_name,
                status=status,
                per_test_log_tail=_trim(test_log, max_chars=800),
                container_error_excerpt=_trim(container_log, max_chars=1600),
            )
        )

    total = len(test_results)
    passed = len(passed_names)
    iteration_id = f"{loop}{iteration}"
    summary = (
        f"Functional tests ({loop} iteration {iteration}): {passed}/{total} passed"
        + (f"; failed: {', '.join(failure.name for failure in failures)}" if failures else "")
    )
    code_failure = BuilderCodeFailureRecord(
        phase="code",
        kind="functional_test",
        iteration_id=iteration_id,
        attempt=iteration,
        summary=summary,
        num_passed_ft=passed,
        num_total_ft=total,
        failed_tests=tuple(failures),
        passed_tests=tuple(passed_names),
    )
    return BuilderFunctionalFailureRecord(
        phase="functional",
        loop=loop,
        iteration=iteration,
        implementation_key=implementation_key,
        implementation_digest=implementation_digest(implementation),
        code_failure=code_failure,
        trigger_test=trigger_test,
        judge_verdict=judge_verdict,
    )


def persist_functional_failure(root: str, record: BuilderFunctionalFailureRecord) -> str:
    """Write a builder failure record and return its path."""
    path = functional_failure_path(
        root, record.loop, record.iteration, record.implementation_key
    )
    with open(path, "w", encoding="utf-8") as file:
        json.dump(record.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
    return path

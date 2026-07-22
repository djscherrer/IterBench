"""Typed Locust generation and verification failures."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from failure import FailureRecord, failure_prompt_header, persist_failure_record, trim
from workspace.scenario_builder_paths import locust_failure_path

LocustFailureKind = Literal[
    "model_request",
    "script_parse",
    "script_syntax",
    "invalid_user_class",
    "smoke_contract",
    "reference_implementation_build",
    "reference_application_startup",
    "locust_runtime",
    "stats_missing_or_invalid",
    "endpoint_coverage",
    "unexpected_request_failures",
    "reference_application_unhealthy",
]


def locust_script_digest(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LocustFailureRecord(FailureRecord):
    """One actionable failure from a Locust candidate or its verification run."""

    phase: Literal["performance"]
    kind: LocustFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    retry_target: Literal[
        "author_agent", "implementation", "infrastructure", "unknown"
    ] = "author_agent"
    reference_implementation: str = ""
    script_digest: str = ""
    missing_endpoints: tuple[str, ...] = ()
    request_count: int = 0
    failure_count: int = 0
    diagnostic_excerpt: str = ""

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": 1,
            **self.common_dict(include_null_attempt=False),
            "retry_target": self.retry_target,
        }
        if self.reference_implementation:
            data["reference_implementation"] = self.reference_implementation
        if self.script_digest:
            data["script_digest"] = self.script_digest
        if self.missing_endpoints:
            data["missing_endpoints"] = list(self.missing_endpoints)
        if self.request_count:
            data["request_count"] = self.request_count
        if self.failure_count:
            data["failure_count"] = self.failure_count
        if self.diagnostic_excerpt:
            data["diagnostic_excerpt"] = self.diagnostic_excerpt
        return data

    def short_excerpt(self) -> str:
        return trim(self.diagnostic_excerpt or self.summary, max_chars=1200)

    def to_prompt_block(self) -> str:
        lines = failure_prompt_header(
            stage_label="Locust generation/verification",
            iteration_id=self.iteration_id,
            attempt=self.attempt,
            kind=self.kind,
        )
        lines.extend([self.summary, "", f"- **Retry target**: `{self.retry_target}`"])
        if self.missing_endpoints:
            lines.extend(["", "### OpenAPI operations not reached"])
            lines.extend(f"- `{endpoint}`" for endpoint in self.missing_endpoints)
        if self.request_count:
            lines.append(f"- **Observed requests**: {self.request_count}")
        if self.failure_count:
            lines.append(f"- **Observed failed requests**: {self.failure_count}")
        if self.diagnostic_excerpt:
            lines.extend(
                [
                    "",
                    "### Diagnostic excerpt",
                    "```",
                    trim(self.diagnostic_excerpt, max_chars=1600),
                    "```",
                ]
            )
        if self.retry_target == "author_agent":
            lines.extend(
                [
                    "",
                    "The previous complete script is already in this conversation. "
                    "Revise it; do not repeat the scenario or prior script in prose.",
                ]
            )
        return "\n".join(lines)


def persist_locust_failure(root: str, record: LocustFailureRecord) -> str:
    attempt = record.attempt if record.attempt is not None else 0
    return str(
        persist_failure_record(locust_failure_path(root, attempt, record.kind), record)
    )

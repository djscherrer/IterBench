"""Typed failures emitted while authoring a new scenario."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from failure import FailureRecord, failure_prompt_header, persist_failure_record, trim
from workspace.scenario_builder_paths import generation_failure_path

ScenarioGenerationStage = Literal["idea", "novelty", "openapi", "text_spec"]
ScenarioGenerationFailureKind = Literal[
    "model_request",
    "idea_parse",
    "novelty_parse",
    "novelty_duplicate",
    "novelty_inconclusive",
    "openapi_format",
    "openapi_yaml",
    "openapi_validation",
    "text_spec_format",
]


@dataclass(frozen=True)
class ScenarioGenerationFailureRecord(FailureRecord):
    """A concise, K8s-compatible record for one scenario-authoring failure."""

    phase: Literal["scenario_generation"]
    kind: ScenarioGenerationFailureKind
    iteration_id: str
    summary: str
    attempt: int | None = None
    stage: ScenarioGenerationStage = "idea"
    candidate_title: str = ""
    candidate_description: str = ""
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    diagnostic_excerpt: str = ""
    retry_target: Literal["author_agent", "unknown"] = "author_agent"

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": 1,
            **self.common_dict(include_null_attempt=False),
            "stage": self.stage,
            "retry_target": self.retry_target,
        }
        if self.candidate_title:
            data["candidate_title"] = self.candidate_title
        if self.candidate_description:
            data["candidate_description"] = self.candidate_description
        if self.errors:
            data["errors"] = list(self.errors)
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.diagnostic_excerpt:
            data["diagnostic_excerpt"] = self.diagnostic_excerpt
        return data

    def short_excerpt(self) -> str:
        details = "\n".join(self.errors) or self.diagnostic_excerpt or self.summary
        return trim(details, max_chars=1200)

    def to_prompt_block(self) -> str:
        lines = failure_prompt_header(
            stage_label=f"Scenario {self.stage.replace('_', ' ')} stage",
            iteration_id=self.iteration_id,
            attempt=self.attempt,
            kind=self.kind,
        )
        lines.append(self.summary)
        if self.errors:
            lines.extend(["", "### Validation feedback"])
            lines.extend(f"- {error}" for error in self.errors)
        if self.warnings:
            lines.extend(["", "### Warnings"])
            lines.extend(f"- {warning}" for warning in self.warnings)
        if self.diagnostic_excerpt and not self.errors:
            lines.extend(
                [
                    "",
                    "### Diagnostic excerpt",
                    "```",
                    trim(self.diagnostic_excerpt, max_chars=1600),
                    "```",
                ]
            )
        return "\n".join(lines)


def persist_generation_failure(
    root: str, run_id: str, record: ScenarioGenerationFailureRecord
) -> str:
    """Persist a record beneath the pre-title generation-run artifact root."""
    attempt = record.attempt if record.attempt is not None else 0
    return str(
        persist_failure_record(
            generation_failure_path(root, run_id, record.stage, attempt, record.kind),
            record,
        )
    )

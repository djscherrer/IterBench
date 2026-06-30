"""Shared helpers for slim conversational k8s experiment prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace import k8s_fallback_code_dir, latest_code_dir, latest_spec_path

DECISION_TELEMETRY_POINTER = (
    "Load test results, diagnostics, and failed-attempt anti-examples: see the "
    "**decision-phase user message** immediately above in this conversation."
)

DECISION_GUARDRAILS = """\
If the benchmark feedback below lists **failed attempts since the last successful iteration**, treat them as anti-examples: do not repeat the same change without addressing the recorded failure.

**Do not choose `code`** when a failure is tagged `[INFRASTRUCTURE FAILURE]` due to the test harness itself (Docker port conflict, container networking failure, image pull error, Postgres test container failing to start). In those cases, rewriting application code will not help; pick `deployment` (or rerun) to adjust resources/config.

Note: **"Server did not start in time" is ambiguous** — it can be true infrastructure (container never started) *or* a fast application crash at import/startup. Use the accompanying container logs/traceback to decide; if it crashed due to a Python exception, choose `code` and fix the crash."""


@dataclass(frozen=True)
class ArtifactPointers:
    """Iteration folder names for conversation-history artifact references."""

    code_iteration_folder: str
    spec_iteration_folder: str | None


def resolve_artifact_pointers(
    sample_dir: Path, *, experiment_id: str | None = None
) -> ArtifactPointers:
    """Map on-disk code/spec lineage to iteration folder names for prompt pointers."""
    code_dir = latest_code_dir(
        sample_dir,
        fallback=k8s_fallback_code_dir(sample_dir, experiment_id=experiment_id),
        experiment_id=experiment_id,
    )
    code_folder = code_dir.parent.parent.name

    spec_pair = latest_spec_path(sample_dir, experiment_id=experiment_id)
    spec_folder = spec_pair[1].name if spec_pair is not None else None

    return ArtifactPointers(
        code_iteration_folder=code_folder,
        spec_iteration_folder=spec_folder,
    )


def format_artifact_pointers_block(pointers: ArtifactPointers) -> str:
    """Bullet block pointing at codegen / SPEC turns in conversation history."""
    lines = [
        f"- **Application code**: see your codegen response from "
        f"`{pointers.code_iteration_folder}` in conversation history.",
    ]
    if pointers.spec_iteration_folder:
        lines.append(
            f"- **Kubernetes deployment**: see your `<SPEC>` response from "
            f"`{pointers.spec_iteration_folder}` in conversation history."
        )
    else:
        lines.append(
            "- **Kubernetes deployment**: (no prior `<SPEC>` in conversation history yet)"
        )
    return "\n".join(lines)

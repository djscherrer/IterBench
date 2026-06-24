"""Shared helpers for slim conversational k8s experiment prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace import k8s_fallback_code_dir, latest_code_dir, latest_spec_path
    "Load test results, diagnostics, and failed-attempt anti-examples: see the "
    "**decision-phase user message** immediately above in this conversation."
)

DECISION_GUARDRAILS = """\
If the benchmark feedback below lists **failed attempts since the last successful iteration**, treat them as anti-examples: do not repeat the same change without addressing the recorded failure.

**Do not choose `code`** when a failure is tagged `[INFRASTRUCTURE FAILURE]` (Docker port conflict, container start failure, image pull error, or "Server did not start in time"). The functional-test harness blocked before HTTP reached the application — rewriting application code cannot fix that; pick `deployment` to retry deploy or adjust resources."""


@dataclass(frozen=True)
class ArtifactPointers:
    """Iteration folder names for conversation-history artifact references."""

    code_iteration_folder: str
    spec_iteration_folder: str | None


def resolve_artifact_pointers(sample_dir: Path) -> ArtifactPointers:
    """Map on-disk code/spec lineage to iteration folder names for prompt pointers."""
    code_dir = latest_code_dir(
        sample_dir, fallback=k8s_fallback_code_dir(sample_dir)
    )
    code_folder = code_dir.parent.parent.name

    spec_pair = latest_spec_path(sample_dir)
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

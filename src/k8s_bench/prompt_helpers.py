"""Shared helpers for slim conversational k8s experiment prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace import k8s_fallback_code_dir, latest_code_dir, latest_spec_path


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


def format_artifact_pointers_block(
    pointers: ArtifactPointers,
    *,
    include_bench_telemetry: bool = False,
) -> str:
    """Bullet block pointing at prior turns in conversation history."""
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
    if include_bench_telemetry:
        lines.append(
            "- **Benchmark telemetry**: load-test results, diagnostics, and "
            "failed-attempt anti-examples — see the **decision-phase** user "
            "message in conversation history (where deployment vs code was chosen)."
        )
    return "\n".join(lines)

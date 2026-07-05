"""Shared helpers for slim conversational k8s experiment prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .failure import CodeFailureRecord, load_terminal_failure_record
from .workspace import (
    find_latest_code_snapshot_iteration,
    iteration_folder_is_failed,
    iteration_functional_tests_dir,
    iteration_id_for_index,
    latest_spec_path,
    resolve_iteration_dir,
)


@dataclass(frozen=True)
class ArtifactPointers:
    """Iteration folder names for conversation-history artifact references."""

    code_iteration_folder: str
    code_status: str
    spec_iteration_folder: str | None


def _code_pointer_status(iteration_path: Path) -> str:
    """Short parenthetical for prompt pointers, e.g. ``failed: did not compile``."""
    if iteration_folder_is_failed(iteration_path.name):
        record = load_terminal_failure_record(iteration_path, phase="code")
        if isinstance(record, CodeFailureRecord):
            if record.kind in {"docker_build", "llm_parse"} or (
                record.diagnostic_excerpt
                and any(
                    marker in record.diagnostic_excerpt
                    for marker in ("error[E", "could not compile", "npm ERR")
                )
            ):
                return "failed: did not compile"
            if record.failed_tests:
                return (
                    f"failed: {record.num_passed_ft}/{record.num_total_ft} "
                    f"functional tests passing"
                )
            if record.kind == "infrastructure":
                return "failed: infrastructure"
        return "failed"

    from .code.shared import functional_tests_passed_at

    ft_results = iteration_functional_tests_dir(iteration_path) / "test_results.json"
    if functional_tests_passed_at(ft_results):
        return "passed functional tests"
    return ""


def resolve_artifact_pointers(
    sample_dir: Path, *, experiment_id: str | None = None
) -> ArtifactPointers:
    """Map on-disk code/spec lineage to iteration folder names for prompt pointers."""
    latest_iteration = find_latest_code_snapshot_iteration(
        sample_dir, experiment_id=experiment_id
    )
    if latest_iteration is None:
        latest_iteration = resolve_iteration_dir(
            sample_dir,
            iteration_id_for_index(0),
            experiment_id=experiment_id,
        )

    spec_pair = latest_spec_path(sample_dir, experiment_id=experiment_id)
    spec_folder = spec_pair[1].name if spec_pair is not None else None

    return ArtifactPointers(
        code_iteration_folder=latest_iteration.name,
        code_status=_code_pointer_status(latest_iteration),
        spec_iteration_folder=spec_folder,
    )


def format_code_history_pointer(
    iteration_folder: str,
    *,
    status: str = "",
) -> str:
    """One bullet pointing at a prior ``<CODE>`` turn in conversation history."""
    line = (
        f"- **Application code**: see your `<CODE>` response from "
        f"`{iteration_folder}` in conversation history"
    )
    if status:
        line += f" ({status})"
    return f"{line}."


def format_artifact_pointers_block(
    pointers: ArtifactPointers,
    *,
    include_bench_telemetry: bool = False,
) -> str:
    """Bullet block pointing at prior turns in conversation history."""
    lines = [
        format_code_history_pointer(
            pointers.code_iteration_folder,
            status=pointers.code_status,
        ),
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

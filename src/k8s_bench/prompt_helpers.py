"""Shared helpers for slim conversational k8s experiment prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .failure import CodeFailureRecord, load_terminal_failure_record
from .workspace import (
    find_last_code_refinement_iteration,
    find_last_spec_refinement_iteration,
    iteration_folder_is_failed,
    iteration_functional_tests_dir,
    iteration_id_for_index,
    resolve_iteration_dir,
)

PointerScope = Literal["decision", "code", "spec"]


@dataclass(frozen=True)
class ArtifactPointers:
    """Iteration folder names for conversation-history artifact references."""

    code_iteration_folder: str | None
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
    sample_dir: Path,
    *,
    iteration_index: int,
    experiment_id: str | None = None,
    scope: PointerScope = "decision",
) -> ArtifactPointers:
    """
    Map on-disk lineage to conversation-history artifact references.

    Pointers aim at the artifact being edited in the current phase:
    - **code** scope → last code-refinement iteration (usually N−1 on a code path)
    - **spec** scope → last spec-refinement iteration (skips deploy-only copies)
    - **decision** scope → both of the above
    """
    code_folder: str | None = None
    code_status = ""
    if scope in {"decision", "code"}:
        code_iter = find_last_code_refinement_iteration(
            sample_dir, iteration_index, experiment_id=experiment_id
        )
        if code_iter is None and iteration_index > 0:
            code_iter = resolve_iteration_dir(
                sample_dir,
                iteration_id_for_index(0),
                experiment_id=experiment_id,
            )
        if code_iter is not None:
            code_folder = code_iter.name
            code_status = _code_pointer_status(code_iter)

    spec_folder: str | None = None
    if scope in {"decision", "spec"}:
        spec_iter = find_last_spec_refinement_iteration(
            sample_dir, iteration_index, experiment_id=experiment_id
        )
        if spec_iter is not None:
            spec_folder = spec_iter.name

    return ArtifactPointers(
        code_iteration_folder=code_folder,
        code_status=code_status,
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
    scope: PointerScope = "decision",
) -> str:
    """Bullet block pointing at prior turns in conversation history."""
    lines: list[str] = []
    if scope in {"decision", "code"}:
        if pointers.code_iteration_folder:
            lines.append(
                format_code_history_pointer(
                    pointers.code_iteration_folder,
                    status=pointers.code_status,
                )
            )
        else:
            lines.append(
                "- **Application code**: (no prior `<CODE>` in conversation history yet)"
            )
    if scope in {"decision", "spec"}:
        if pointers.spec_iteration_folder:
            lines.append(
                f"- **Kubernetes deployment**: see your `<SPEC>` response from "
                f"`{pointers.spec_iteration_folder}` in conversation history."
            )
        else:
            lines.append(
                "- **Kubernetes deployment**: "
                "(no prior `<SPEC>` in conversation history yet)"
            )
    return "\n".join(lines)

"""Prompt builders for baseline and refinement code generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..feedback import IterationFeedback
from ..failure import FunctionalFailureReport
from ..prompt_helpers import (
    format_artifact_pointers_block,
    resolve_artifact_pointers,
)

_CODEGEN_ROLE = (
    "You are an application engineer for BaxBench iterative Kubernetes "
    "performance experiments."
)


def _application_requirements(task: Any) -> str:
    """Standard BaxBench codegen template (OpenAPI, framework, packages, output format)."""
    return task.scenario.build_prompt(
        task.env,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        agent=False,
        use_stubs=task.use_stubs,
    )


def _codegen_experiment_preamble(
    *,
    task: Any,
    iteration_id: str,
    iteration_index: int,
    total_iterations: int,
    goal: str,
    progress_note: str = "",
) -> str:
    from ..spec.prompts import _safety_performance_text, format_iteration_progress

    progress = format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    perf = _safety_performance_text(task.env, task.scenario, task.safety_prompt)
    progress_line = f"**Progress**: {progress}"
    if progress_note:
        progress_line = f"{progress_line} {progress_note}"
    return "\n".join(
        [
            _CODEGEN_ROLE,
            "",
            "## Goal",
            goal,
            "",
            progress_line,
            "",
            f"**Optimization objective**: ",
            "Maximize **goodput** (successful HTTP responses per second). The app runs in "
            "a container on Kubernetes under adaptive Locust load with many concurrent users. "
            "Failed requests do not count toward your score. Write for sustained throughput — "
            "efficient DB access, safe schema init under gunicorn `--preload`, connection "
            "pooling — not just minimal correctness.",
            "",
            perf,
            "",
            "## Context",
            f"- Scenario: {task.scenario.id}",
            f"- Environment: {task.env.id} (listen port {task.env.port})",
            f"- Database required: {task.scenario.needs_db}",
            f"- Iteration: {iteration_id}",
        ]
    )


def baseline_retry_feedback_block(
    *,
    prior_code: str,
    failure_report: FunctionalFailureReport,
) -> str:
    block = failure_report.to_prompt_block()
    parts = [
        "## Your previous attempt failed the functional tests — fix it",
        "",
        "The program you generated on the previous attempt is shown below. It "
        "did not pass the functional tests. Produce a **complete corrected "
        "program** (same output format as before). The functional test source "
        "is intentionally withheld — implement the behaviour the API spec "
        "requires; do not try to special-case the tests.",
        "",
        "### Your previous code",
        prior_code.strip() or "(previous code unavailable)",
        "",
        "### What failed",
        block or "(no structured failure detail captured)",
    ]
    return "\n".join(parts)


def build_baseline_prompt(
    *,
    task: Any,
    iteration_id: str,
    iteration_index: int = 0,
    total_iterations: int = 0,
    prior_code: str = "",
    failure_report: FunctionalFailureReport | None = None,
) -> str:
    parts = [
        _codegen_experiment_preamble(
            task=task,
            iteration_id=iteration_id,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            goal=(
                f"Implement the application for iteration `{iteration_id}` so it "
                "**passes functional tests** and performs well once deployed."
            ),
        ),
        "",
        _application_requirements(task),
    ]
    if failure_report is not None:
        parts.extend(
            [
                "",
                baseline_retry_feedback_block(
                    prior_code=prior_code, failure_report=failure_report
                ),
            ]
        )
    return "\n".join(parts)


def build_code_refinement_prompt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_id: str,
    prior_feedback: IterationFeedback,
    same_iteration_failure_report: FunctionalFailureReport | None = None,
    prior_failure_report: FunctionalFailureReport | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
    experiment_id: str | None = None,
) -> str:
    del prior_feedback  # pointers replace inline feedback in slim prompts
    sample_dir = task.get_sample_dir(results_dir, sample)
    pointers = resolve_artifact_pointers(
        sample_dir, experiment_id=experiment_id
    )
    pointer_block = format_artifact_pointers_block(pointers)

    parts = [
        _codegen_experiment_preamble(
            task=task,
            iteration_id=iteration_id,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
            goal=(
                f"Improve the **application source code** for iteration `{iteration_id}` "
                "using the starting codebase referenced below. New code must pass "
                "functional tests. The **deployment spec stays unchanged** in this "
                "iteration."
            ),
            progress_note=(
                "Budget your remaining iterations accordingly — pick the changes most "
                "likely to lift goodput within what is left."
            ),
        ),
        "",
        "Keep the same API contract and scenario requirements. Output a complete "
        "replacement codebase using the same `<FILEPATH>` / `<CODE>` format as initial "
        "generation.",
        "",
        pointer_block,
        "",
        _application_requirements(task),
    ]

    if prior_failure_report is not None:
        prior_block = prior_failure_report.to_prompt_block()
        if prior_block:
            parts.extend(
                [
                    "",
                    "### Previous code-refinement attempt failed (this is a "
                    "**must-fix** signal — do not produce another revision "
                    "that breaks the same tests)",
                    "",
                    prior_block,
                ]
            )

    if same_iteration_failure_report is not None:
        same_block = same_iteration_failure_report.to_prompt_block()
        if same_block:
            parts.extend(
                [
                    "",
                    "### Functional test feedback from this iteration's previous codegen attempt",
                    "(your most recent regeneration within this same iteration failed "
                    "these tests; fix them)",
                    "",
                    same_block,
                ]
            )

    return "\n".join(parts)

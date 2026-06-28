"""Prompt builders for baseline and refinement code generation."""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any

from llm import Prompter

from ..feedback import IterationFeedback
from ..failure import FunctionalFailureReport
from ..prompt_helpers import (
    DECISION_TELEMETRY_POINTER,
    format_artifact_pointers_block,
    resolve_artifact_pointers,
)
from .deployment_context import format_k8s_deployment_context


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
    prompter: Prompter,
    *,
    prior_code: str = "",
    failure_report: FunctionalFailureReport | None = None,
) -> str:
    prompt_text = prompter.prompt
    if failure_report is not None:
        feedback = baseline_retry_feedback_block(
            prior_code=prior_code, failure_report=failure_report
        )
        prompt_text = f"{prompt_text}\n\n{feedback}"
    return prompt_text


def build_code_refinement_prompt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    prior_feedback: IterationFeedback,
    same_iteration_failure_report: FunctionalFailureReport | None = None,
    prior_failure_report: FunctionalFailureReport | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
) -> str:
    del prior_feedback  # pointers replace inline feedback in slim prompts
    sample_dir = task.get_sample_dir(results_dir, sample)
    pointers = resolve_artifact_pointers(sample_dir)
    base = task.scenario.build_prompt(
        task.env,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        agent=False,
        use_stubs=task.use_stubs,
    )

    from ..spec.prompts import format_iteration_progress

    progress = format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    pointer_block = format_artifact_pointers_block(pointers)
    parts = [
        base,
        "",
        "## Refinement task (k8s benchmark feedback)",
        "",
        f"**Progress**: {progress} Budget your remaining iterations accordingly — pick the changes most likely to lift goodput within what is left.",
        "",
        "**Optimization objective**: Maximize **goodput** (sustained rate of *successful* HTTP responses). Failed requests do not count.",
        "",
        "Improve the **application source code** using the starting codebase referenced below. "
        "New code must pass functional tests. The deployment spec stays unchanged in this iteration.",
        "",
        "Keep the same API contract and scenario requirements. Output a complete replacement "
        "codebase using the same `<FILEPATH>` / `<CODE>` format as initial generation.",
        "",
        "## Context",
        "",
        f"- Scenario: {task.scenario.id}",
        f"- Environment: {task.env.id}",
        f"- Iteration: {iteration_path.name}",
        "",
        pointer_block,
        "",
        DECISION_TELEMETRY_POINTER,
        "",
    ]
    replica_hint = format_k8s_deployment_context(iteration_path, sample_dir)
    if replica_hint:
        parts.extend([replica_hint, ""])

    if prior_failure_report is not None:
        prior_block = prior_failure_report.to_prompt_block()
        if prior_block:
            parts.extend(
                [
                    "### Previous code-refinement attempt failed (this is a "
                    "**must-fix** signal — do not produce another revision "
                    "that breaks the same tests)",
                    "",
                    prior_block,
                    "",
                ]
            )

    if same_iteration_failure_report is not None:
        same_block = same_iteration_failure_report.to_prompt_block()
        if same_block:
            parts.extend(
                [
                    "### Functional test feedback from this iteration's previous codegen attempt",
                    "(your most recent regeneration within this same iteration failed these tests; fix them)",
                    "",
                    same_block,
                ]
            )
    return "\n".join(parts)


def call_llm_with_retries(
    *,
    prompter: Prompter,
    prompt_text: str,
    sample_dir: Path,
    logger: logging.Logger,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    log_label: str,
) -> str:
    from ..session import persist_session

    retries = 0
    while True:
        try:
            raw = prompter.send(prompt_text, logger)
            persist_session(prompter, sample_dir, logger=logger)
            return raw
        except Exception as exc:
            retries += 1
            if retries > max_retries:
                logger.error("%s LLM call failed after retries: %s", log_label, exc)
                raise
            delay = min(base_delay * 2**retries, max_delay)
            delay = random.uniform(0, delay)
            logger.warning(
                "%s LLM attempt %d/%d failed: %s; retry in %.1fs",
                log_label,
                retries,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

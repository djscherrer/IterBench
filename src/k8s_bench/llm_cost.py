"""K8s benchmark LLM cost recording (delegates to ``llm.usage``)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from llm.usage import (
    LlmUsageRecord,
    enforce_cost_budget,
    ensure_cost_section_in_summary,
    record_prompter_usage,
)

from .experiment_summary import experiment_summary_path
from workspace import k8s_workspace_root


def k8s_llm_ledger_dir(
    sample_dir: Path,
    *,
    experiment_id: str = "default",
) -> Path:
    """Experiment workspace root where ``llm_cost_ledger.json`` lives."""
    return k8s_workspace_root(sample_dir, experiment_id=experiment_id)


def check_k8s_llm_budget(
    sample_dir: Path,
    *,
    experiment_id: str = "default",
    max_cost_usd: float | None = None,
) -> None:
    """Raise if estimated spend already exceeds ``max_cost_usd``."""
    enforce_cost_budget(
        k8s_llm_ledger_dir(sample_dir, experiment_id=experiment_id),
        max_cost_usd=max_cost_usd,
    )


def record_k8s_llm_call(
    *,
    prompter: Any,
    call_type: str,
    sample_dir: Path,
    logger: logging.Logger,
    artifact_dir: Path | None = None,
    iteration_id: str | None = None,
    note: str | None = None,
    experiment_id: str = "default",
    max_cost_usd: float | None = None,
) -> LlmUsageRecord | None:
    """
    Persist token usage for one k8s-bench LLM call.

    Appends to ``<k8s_workspace>/llm_cost_ledger.json`` and optionally writes
    ``artifact_dir/llm_usage.json``. Honors ``max_cost_usd`` when set.
    """
    workspace = k8s_llm_ledger_dir(sample_dir, experiment_id=experiment_id)
    record = record_prompter_usage(
        prompter=prompter,
        call_type=call_type,
        workspace=workspace,
        logger=logger,
        artifact_dir=artifact_dir,
        iteration_id=iteration_id,
        note=note,
        max_cost_usd=max_cost_usd,
    )
    refresh_k8s_cost_summary(sample_dir, experiment_id=experiment_id)
    return record


def refresh_k8s_cost_summary(
    sample_dir: Path,
    *,
    experiment_id: str = "default",
) -> None:
    """Insert or update the cost block in ``experiment_summary.md``."""
    summary = experiment_summary_path(sample_dir, experiment_id=experiment_id)
    if not summary.is_file():
        return
    ensure_cost_section_in_summary(
        summary,
        k8s_llm_ledger_dir(sample_dir, experiment_id=experiment_id),
    )

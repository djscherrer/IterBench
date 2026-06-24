"""LLM decision stage: refine deployment spec vs application code."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..feedback import IterationFeedback
from ..prompt_helpers import (
    DECISION_GUARDRAILS,
    format_artifact_pointers_block,
    resolve_artifact_pointers,
)

RefinementAction = Literal["deployment", "code"]
RefinementMode = Literal["auto", "deployment", "code", "off"]

_DECISION_RE = re.compile(
    r"<DECISION>\s*(deployment|code)\s*</DECISION>", re.IGNORECASE | re.DOTALL
)
_RATIONALE_RE = re.compile(
    r"<RATIONALE>\s*(.*?)\s*</RATIONALE>", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class RefinementDecision:
    action: RefinementAction
    rationale: str
    raw_response: str
    iteration_index: int
    based_on_iteration: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rationale": self.rationale,
            "iteration_index": self.iteration_index,
            "based_on_iteration": self.based_on_iteration,
            "raw_response": self.raw_response,
        }


def resolve_refinement_mode(explicit: str | None = None) -> RefinementMode:
    raw = (
        (explicit or "").strip()
        or os.environ.get("BAXBENCH_K8S_REFINEMENT", "auto").strip()
        or "auto"
    ).lower()
    if raw in {"off", "false", "none", "0"}:
        return "off"
    if raw in {"auto", "deployment", "code"}:
        return raw  # type: ignore[return-value]
    raise ValueError(
        f"Invalid k8s refinement mode {raw!r}; use auto, deployment, code, or off"
    )


def build_refinement_decision_prompt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    prior_feedback: IterationFeedback,
    iteration_index: int,
    next_iteration_id: str,
    total_iterations: int = 0,
) -> str:
    sample_dir = task.get_sample_dir(results_dir, sample)
    pointers = resolve_artifact_pointers(sample_dir)
    from ..spec.generation import _format_iteration_progress

    progress = _format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    pointer_block = format_artifact_pointers_block(pointers)
    feedback_text = prior_feedback.to_prompt_text(include_spec_yaml=False)
    return f"""You are a performance optimization strategist for BaxBench iterative experiments.

After iteration `{prior_feedback.iteration_id}`, decide what to refine **next** (`{next_iteration_id}`) to improve benchmark **goodput** (sustained rate of *successful* HTTP responses; failed requests do not count).

**Progress**: {progress} Choose the path most likely to lift goodput within the remaining budget.

You may choose **exactly one** path:

1. **`deployment`** — tune Kubernetes deployment parameters only. Levers include backend replicas, concurrency, and CPU/memory; database replicas and resources/GUCs; PgBouncer `pooler` and `read_pooler`; optional Redis `cache`; and pod `placement`. The application source code stays unchanged.
2. **`code`** — improve the **application source code** (performance, error handling, DB usage, concurrency). New code must pass functional tests. The deployment spec stays unchanged in this iteration.

{DECISION_GUARDRAILS}

## Context

- Scenario: {task.scenario.id}
- Environment: {task.env.id}
- Iteration: {next_iteration_id}

{pointer_block}

## Benchmark feedback (previous iteration)

{feedback_text}

## Output format

Return exactly:

<DECISION>
deployment
</DECISION>

<RATIONALE>
One short paragraph explaining why this path should help next.
</RATIONALE>

Use `deployment` or `code` (lowercase) inside `<DECISION>`.
"""


def parse_refinement_decision(
    response: str,
    *,
    iteration_index: int,
    based_on_iteration: str,
) -> RefinementDecision:
    match = _DECISION_RE.search(response)
    if not match:
        raise ValueError(
            "Model response did not contain <DECISION>deployment|code</DECISION>"
        )
    action = match.group(1).strip().lower()
    if action not in {"deployment", "code"}:
        raise ValueError(f"Invalid refinement action: {action!r}")
    rationale_match = _RATIONALE_RE.search(response)
    rationale = (
        rationale_match.group(1).strip()
        if rationale_match
        else "(no rationale block)"
    )
    return RefinementDecision(
        action=action,  # type: ignore[arg-type]
        rationale=rationale,
        raw_response=response,
        iteration_index=iteration_index,
        based_on_iteration=based_on_iteration,
    )


def decide_refinement_action(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    prior_feedback: IterationFeedback,
    iteration_index: int,
    next_iteration_id: str,
    logger: logging.Logger,
    vllm_port: int = 8000,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    total_iterations: int = 0,
) -> RefinementDecision:
    from ..workspace import PROMPT_LOG_FILENAME, iteration_decision_dir

    prompt = build_refinement_decision_prompt(
        task=task,
        results_dir=results_dir,
        sample=sample,
        prior_feedback=prior_feedback,
        iteration_index=iteration_index,
        next_iteration_id=next_iteration_id,
        total_iterations=total_iterations,
    )
    logger.info("refinement decision prompt:\n%s", prompt)
    decision_dir = iteration_decision_dir(iteration_path)
    decision_dir.mkdir(parents=True, exist_ok=True)
    (decision_dir / PROMPT_LOG_FILENAME).write_text(prompt + "\n", encoding="utf-8")

    from ..session import get_experiment_session, persist_session

    sample_dir = task.get_sample_dir(results_dir, sample)
    prompter = get_experiment_session(
        task, sample_dir, sample, vllm_port=vllm_port, logger=logger
    )

    import random
    import time

    last_raw = ""
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

            check_k8s_llm_budget(sample_dir)
            last_raw = prompter.send(prompt, logger)
            record_k8s_llm_call(
                prompter=prompter,
                call_type="refinement_decision",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=iteration_decision_dir(iteration_path),
                iteration_id=next_iteration_id,
            )
            persist_session(prompter, sample_dir, logger=logger)
            return parse_refinement_decision(
                last_raw,
                iteration_index=iteration_index,
                based_on_iteration=prior_feedback.iteration_id,
            )
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            delay = min(base_delay * 2**attempt, max_delay)
            delay = random.uniform(0, delay)
            logger.warning(
                "refinement decision attempt %d/%d failed: %s; retry in %.1fs",
                attempt,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

    logger.warning(
        "refinement decision failed after %d attempts; defaulting to deployment: %s",
        max_retries,
        last_exc,
    )
    return RefinementDecision(
        action="deployment",
        rationale=f"Decision LLM failed ({last_exc}); defaulting to deployment tuning.",
        raw_response=last_raw,
        iteration_index=iteration_index,
        based_on_iteration=prior_feedback.iteration_id,
    )


def _folder_kind(action: str) -> str:
    if action == "baseline":
        return "baseline"
    return "code" if action == "code" else "spec"


def run_decision_stage(
    ctx: "SampleContext",
    setup: "IterationSetup",
    cfg: "RunConfig",
) -> "IterationPlan":
    """
    Run the ``01-decision`` stage: choose refinement path and rename the folder.

    Baseline iterations skip the LLM; refinement iterations force or call the
    decision LLM, then apply the ``-baseline`` / ``-spec`` / ``-code`` suffix.
    """
    from ..workspace import (
        apply_iteration_folder_suffix,
        iteration_decision_log_path,
        latest_code_dir,
        k8s_fallback_code_dir,
        update_iteration_meta,
    )
    from ..code.prior import find_latest_prior_failure_report
    from ..orchestration.config import (
        IterationPlan,
        PriorIteration,
        RefinementAction,
    )

    refinement_action: RefinementAction = (
        "baseline" if setup.is_baseline else "deployment"
    )
    prior = setup.prior
    decision: RefinementDecision | None = None
    iteration_path = setup.iteration_path

    decision_log = iteration_decision_log_path(iteration_path)
    with ctx.task.create_logger(decision_log) as iteration_logger:
        decision = _decide_refinement(
            ctx,
            iteration_path,
            setup.iteration_index,
            setup.iteration_id,
            cfg,
            prior,
            setup.is_baseline,
            iteration_logger,
        )
        if decision is not None:
            refinement_action = (
                decision.action if decision.action != "deployment" else "deployment"
            )
            if decision.action == "code":
                refinement_action = "code"

        iteration_path = apply_iteration_folder_suffix(
            iteration_path, _folder_kind(refinement_action)
        )
        update_iteration_meta(iteration_path, folder=iteration_path.name)

        baseline_code = k8s_fallback_code_dir(ctx.sample_dir)
        source_code_dir = latest_code_dir(ctx.sample_dir, fallback=baseline_code)

        reuse_spec_from: str | None = None
        if refinement_action == "code" and prior.bench_feedback is not None:
            reuse_spec_from = prior.bench_feedback.iteration_id

        if refinement_action == "code":
            failure_report = find_latest_prior_failure_report(
                ctx.sample_dir, current_iteration_index=setup.iteration_index
            )
            prior = PriorIteration(
                bench_feedback=prior.bench_feedback,
                failure_report=failure_report,
            )
            if failure_report is not None:
                iteration_logger.info(
                    "iteration %s: prior code-refinement failure detected in %s "
                    "(%d/%d FT passed, failed=%s); will surface in prompt",
                    setup.iteration_id,
                    failure_report.iteration_id,
                    failure_report.num_passed_ft,
                    failure_report.num_total_ft,
                    [ft.name for ft in failure_report.failed_tests] or "(unknown)",
                )

    return IterationPlan(
        iteration_id=setup.iteration_id,
        iteration_index=setup.iteration_index,
        refinement_action=refinement_action,
        decision=decision,
        prior=prior,
        reuse_spec_from=reuse_spec_from,
        source_code_dir=source_code_dir,
    )


def _decide_refinement(
    ctx: "SampleContext",
    iteration_path: Path,
    iteration_index: int,
    iteration_id: str,
    cfg: "RunConfig",
    prior: "PriorIteration",
    is_baseline: bool,
    logger: logging.Logger,
) -> RefinementDecision | None:
    """Run/force the refinement decision and persist it to disk."""
    from ..workspace import update_iteration_meta, write_decision

    if is_baseline or cfg.refinement_mode == "off":
        return None

    if prior.bench_feedback is None:
        logger.warning(
            "iteration %s: no benchmark feedback from prior iterations; "
            "defaulting to spec tuning (%s mode)",
            iteration_id,
            cfg.refinement_mode,
        )
        decision = RefinementDecision(
            action="deployment",
            rationale=(
                "No benchmark feedback from prior iterations; "
                "defaulting to deployment/spec tuning."
            ),
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration="",
        )
        write_decision(iteration_path, decision)
        update_iteration_meta(iteration_path, refinement_action="deployment")
        return decision

    if cfg.refinement_mode == "code":
        decision = RefinementDecision(
            action="code",
            rationale=f"Forced by refinement mode={cfg.refinement_mode!r}",
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration=prior.bench_feedback.iteration_id,
        )
    elif cfg.refinement_mode == "deployment":
        decision = RefinementDecision(
            action="deployment",
            rationale=f"Forced by refinement mode={cfg.refinement_mode!r}",
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration=prior.bench_feedback.iteration_id,
        )
    else:
        decision = decide_refinement_action(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            prior_feedback=prior.bench_feedback,
            iteration_index=iteration_index,
            next_iteration_id=iteration_id,
            logger=logger,
            vllm_port=cfg.vllm_port,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            total_iterations=cfg.total_iterations,
        )

    write_decision(iteration_path, decision)
    update_iteration_meta(
        iteration_path,
        refinement_action=decision.action,
        based_on_iteration=prior.bench_feedback.iteration_id,
    )
    try:
        from ..experiment_summary import append_refinement_decision_block

        append_refinement_decision_block(
            sample_dir=ctx.sample_dir,
            iteration_id=iteration_id,
            decision=decision,
            load_profile=cfg.load_profile,
        )
    except Exception as exc:
        logger.warning(
            "Could not update experiment summary (decision): %s", exc
        )

    if decision.action == "code":
        logger.info(
            "iteration %s: will refine application code after folder setup",
            iteration_id,
        )
    return decision


if TYPE_CHECKING:
    from ..orchestration.config import (
        IterationPlan,
        IterationSetup,
        PriorIteration,
        RunConfig,
        SampleContext,
    )

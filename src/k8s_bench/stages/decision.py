"""LLM decision stage: refine deployment spec vs application code."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..feedback import IterationFeedback
from ..prompt_helpers import format_artifact_pointers_block, resolve_artifact_pointers

RefinementAction = Literal["deployment", "code"]
RefinementMode = Literal["auto", "deployment", "code"]

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


def forced_refinement_action_after_failure(
    failure_kind: str,
) -> RefinementAction | None:
    """
    When iteration N−1 failed, optionally force the same refinement path.

    Returns ``None`` when the strategist LLM should decide (deploy failure,
    successful N−1 handled elsewhere, unknown kinds).
    """
    if failure_kind == "code":
        return "code"
    if failure_kind == "spec":
        return "deployment"
    return None


def persist_refinement_decision(
    ctx: "SampleContext",
    iteration_path: Path,
    iteration_id: str,
    decision: RefinementDecision,
    cfg: "RunConfig",
    logger: logging.Logger,
    *,
    based_on_iteration: str,
) -> None:
    from ..workspace import update_iteration_meta, write_decision

    write_decision(iteration_path, decision)
    update_iteration_meta(
        iteration_path,
        refinement_action=decision.action,
        based_on_iteration=based_on_iteration,
    )
    try:
        from ..experiment_summary import append_refinement_decision_block

        append_refinement_decision_block(
            sample_dir=ctx.sample_dir,
            iteration_id=iteration_id,
            iteration_path=iteration_path,
            decision=decision,
            load_profile=cfg.load_profile,
        )
    except Exception as exc:
        logger.warning(
            "Could not update experiment summary (decision): %s", exc
        )


def resolve_refinement_mode(refinement: str) -> RefinementMode:
    raw = refinement.strip().lower() or "auto"
    if raw in {"auto", "deployment", "code"}:
        return raw  # type: ignore[return-value]
    raise ValueError(
        f"Invalid k8s refinement mode {refinement!r}; use auto, deployment, or code"
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
    experiment_id: str | None = None,
) -> str:
    sample_dir = task.get_sample_dir(results_dir, sample)
    pointers = resolve_artifact_pointers(
        sample_dir, experiment_id=experiment_id
    )
    from ..spec.prompts import format_iteration_progress

    progress = format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    pointer_block = format_artifact_pointers_block(pointers)
    feedback_text = prior_feedback.to_prompt_text()
    return f"""You are a performance optimization strategist for BaxBench iterative experiments.

After iteration `{prior_feedback.iteration_id}`, decide what to refine **next** (`{next_iteration_id}`) to improve benchmark **goodput** (sustained rate of *successful* HTTP responses; failed requests do not count).

**Progress**: {progress} Choose the path most likely to lift goodput within the remaining budget.

You may choose **exactly one** path:

1. **`deployment`** — tune Kubernetes deployment parameters only. Levers include backend replicas, concurrency, and CPU/memory; database replicas and resources/GUCs; PgBouncer `pooler` and `read_pooler`; optional Redis `cache`; and pod `placement`. The application source code stays unchanged.
2. **`code`** — improve the **application source code** (performance, error handling, DB usage, concurrency). New code must pass functional tests. The deployment spec stays unchanged in this iteration.

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
deployment|code
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
    prompter: "Prompter",
    logger: logging.Logger,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    total_iterations: int = 0,
    experiment_id: str = "default",
    llm_max_cost_usd: float | None = None,
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
        experiment_id=experiment_id,
    )
    logger.info("refinement decision prompt:\n%s", prompt)
    decision_dir = iteration_decision_dir(iteration_path)
    decision_dir.mkdir(parents=True, exist_ok=True)
    (decision_dir / PROMPT_LOG_FILENAME).write_text(prompt + "\n", encoding="utf-8")

    sample_dir = task.get_sample_dir(results_dir, sample)
    from ..session import persist_session

    import random
    import time

    last_raw = ""
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

            check_k8s_llm_budget(
                sample_dir,
                experiment_id=experiment_id,
                max_cost_usd=llm_max_cost_usd,
            )
            last_raw = prompter.send(prompt, logger)
            record_k8s_llm_call(
                prompter=prompter,
                call_type="refinement_decision",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=iteration_decision_dir(iteration_path),
                iteration_id=next_iteration_id,
                experiment_id=experiment_id,
                max_cost_usd=llm_max_cost_usd,
            )
            persist_session(
                prompter, sample_dir, experiment_id=experiment_id, logger=logger
            )
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


def run_decision_stage(
    ctx: "SampleContext",
    *,
    iteration_path: Path,
    iteration_index: int,
    iteration_id: str,
    cfg: "RunConfig",
    lineage: "IterationLineage",
    logger: logging.Logger,
) -> RefinementDecision:
    """
    Run the decision stage (``01-decision``): choose code vs deployment-spec refinement.

    Caller must skip this for baseline (iteration_index == 0). This stage does not
    rename the iteration folder or build :class:`~k8s_bench.orchestration.config.IterationPlan`;
    orchestration applies the folder suffix after this returns.
    """
    from ..workspace import update_iteration_meta
    if ctx.session is None:
        raise RuntimeError(
            "missing LLM session on SampleContext; expected sample_preflight() to "
            "initialize ctx.session for iterative experiments"
        )

    if lineage.bench_feedback is None:
        # Hard error: for iteration_index >= 1 we expect iteration N-1 to have
        # either (a) bench feedback artifacts (successful run) or (b) be marked
        # failed, in which case the loader returns an IterationFeedback with
        # status="failed". If both are missing, the experiment directory is in
        # an inconsistent / partially-written state (interrupted run, manual
        # deletion of 05-bench artifacts, etc.).
        from ..workspace import iteration_id_for_index

        prev_id = iteration_id_for_index(iteration_index - 1)
        reason = (
            f"Missing prior benchmark feedback for refinement decision in {iteration_id}: "
            f"expected feedback for previous iteration {prev_id} (either a successful "
            f"bench run under `05-bench/` or a `-failed` marker/meta). "
            "This indicates an inconsistent experiment state (e.g. interrupted run, "
            "incomplete resume, or deleted artifacts). Fix by re-running the previous "
            "iteration with `--force` or deleting the incomplete iteration folder so it "
            "can be regenerated."
        )
        logger.error(reason)
        update_iteration_meta(iteration_path, status="failed", failure_reason=reason)
        raise RuntimeError(reason)

    if cfg.refinement_mode == "code":
        decision = RefinementDecision(
            action="code",
            rationale=f"Forced by refinement mode={cfg.refinement_mode!r}",
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration=lineage.bench_feedback.iteration_id,
        )
    elif cfg.refinement_mode == "deployment":
        decision = RefinementDecision(
            action="deployment",
            rationale=f"Forced by refinement mode={cfg.refinement_mode!r}",
            raw_response="",
            iteration_index=iteration_index,
            based_on_iteration=lineage.bench_feedback.iteration_id,
        )
    else:
        decision = decide_refinement_action(
            task=ctx.task,
            results_dir=ctx.results_dir,
            sample=ctx.sample,
            iteration_path=iteration_path,
            prior_feedback=lineage.bench_feedback,
            iteration_index=iteration_index,
            next_iteration_id=iteration_id,
            prompter=ctx.session,
            logger=logger,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            total_iterations=cfg.total_iterations,
            experiment_id=ctx.experiment_id,
            llm_max_cost_usd=ctx.llm_max_cost_usd,
        )

    persist_refinement_decision(
        ctx,
        iteration_path,
        iteration_id,
        decision,
        cfg,
        logger,
        based_on_iteration=lineage.bench_feedback.iteration_id,
    )

    return decision


if TYPE_CHECKING:
    from llm import Prompter
    from ..orchestration.config import (
        IterationPlan,
        IterationSetup,
        RunConfig,
        SampleContext,
    )
    from ..orchestration.lineage import IterationLineage

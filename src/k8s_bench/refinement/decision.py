"""LLM decision: refine deployment spec vs application code."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from prompts import Prompter

from ..feedback import IterationFeedback
from ..workspace import latest_code_dir
from .code import _read_full_code_for_decision

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
    code_dir = latest_code_dir(
        task.get_sample_dir(results_dir, sample),
        fallback=task.get_code_dir(results_dir, sample),
    )
    full_code = _read_full_code_for_decision(code_dir)
    from ..spec.generation import _format_iteration_progress

    progress = _format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    return f"""You are a performance optimization strategist for BaxBench iterative experiments.

After iteration `{prior_feedback.iteration_id}`, decide what to refine **next** (`{next_iteration_id}`) to improve benchmark **goodput** (sustained rate of *successful* HTTP responses; failed requests do not count).

**Progress**: {progress} Choose the path most likely to lift goodput within the remaining budget.

You may choose **exactly one** path:

1. **`deployment`** — tune Kubernetes deployment parameters only (replicas, CPU/memory, placement, postgres settings). The application source code stays unchanged.
2. **`code`** — improve the **application source code** (performance, error handling, DB usage, concurrency). Deployment will be re-tuned in a later phase after functional tests pass on the new code.

## When to pick each path

Choose **`deployment`** when:
- Errors or saturation look like resource limits, connection pools, replica count, or node placement
- Locust/K8s metrics show CPU/memory throttling or scheduling pressure but the app logic seems sound
- Functional tests already pass and goodput is limited by latency/saturation rather than application bugs

Choose **`code`** when:
- Locust shows application-level errors (5xx, timeouts, logic bugs, DB query issues) suppressing goodput
- Functional tests were passing but perf errors suggest inefficient algorithms, missing indexes, N+1 queries, pool misconfiguration **in code**
- Pod utilization is low yet goodput is poor (software bottleneck)
- The benchmark feedback below lists a recent **code-refinement attempt that failed functional tests** *with concrete application-level errors* — the application is currently broken and must be fixed before any deployment change can help
- The current spec already deploys **Postgres read replicas** (`database.replicas > 1` and `DB_READ_HOST` is set) but replica CPU stays near 0 while the primary saturates — the code must opt into the read pool before any further deployment change can help

**Do NOT choose `code`** when:
- The previous failure block is marked `[INFRASTRUCTURE FAILURE]` or describes a Docker port conflict, container start error, image pull failure, or "Server did not start in time". The functional tests never reached the application in that case — they were *blocked* by the test harness, not failed by the code. Pick `deployment` (you can keep the current spec to retry the harness, or adjust resources) rather than rewriting `app.js`.

If the feedback below lists **failed attempts since the last successful iteration**, treat them as anti-examples: do not repeat the same change without addressing the recorded failure.

## Context

- Scenario: {task.scenario.id}
- Environment: {task.env.id}
- Sample: sample{sample}

## Benchmark feedback (previous iteration)

{prior_feedback.to_prompt_text()}

## Current application code

This is the **full source** the next refinement (if you pick `code`) would start from. Use it to judge whether the bottleneck is in the code (logic / DB usage / concurrency) or in the deployment (resources / replicas / placement). Files are rendered in the same `<FILEPATH>` / `<CODE>` format the refinement model emits back.

{full_code}

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

    prompter = Prompter(
        env=task.env,
        scenario=task.scenario,
        model=task.model,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        batch_size=1,
        offset=0,
        temperature=task.temperature,
        reasoning_effort=task.reasoning_effort,
        vllm_port=vllm_port,
        provider=task.provider,
        use_stubs=task.use_stubs,
    )
    prompter.prompt = prompt

    import random
    import time

    last_raw = ""
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

            check_k8s_llm_budget(task.get_sample_dir(results_dir, sample))
            responses = prompter.prompt_model(logger)
            record_k8s_llm_call(
                prompter=prompter,
                call_type="refinement_decision",
                sample_dir=task.get_sample_dir(results_dir, sample),
                logger=logger,
                artifact_dir=iteration_decision_dir(iteration_path),
                iteration_id=next_iteration_id,
            )
            if not responses:
                raise RuntimeError("LLM returned no completion for refinement decision")
            last_raw = responses[0]
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


# Persistence of :class:`RefinementDecision` now lives in
# ``workspace.artifacts.write_decision``; this module only builds it.

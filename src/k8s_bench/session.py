"""
Persistent, conversational :class:`Prompter` for k8s iterative experiments.

Legacy BaxBench builds a fresh :class:`llm.Prompter` for every LLM call —
each call is a single ``[system, user(prompt)]`` exchange with no memory. For a
k8s *experiment* (one sample, many iterations: baseline codegen → decision →
code/spec refinement → …) we instead want **one conversation per
(sample, experiment)** so the model sees its own prior prompts and responses.

This module provides:

* :func:`get_experiment_session` — return the shared conversational
  ``Prompter`` for a ``(sample_dir, experiment)``, building it once and
  reloading any persisted history from disk so resumed runs continue the
  same thread.
* :func:`persist_session` — write the accumulated history to
  ``<k8s_workspace>/conversation.json`` after each phase.

The ``Prompter`` itself is unchanged for single-shot callers; conversation
mode is opt-in via ``prompter.conversational = True`` (set here). Each phase
appends its prompt + the model reply as turns (via ``Prompter.send`` or
explicit ``append_*``). Prompt slimming for conversational mode lives in
``prompt_helpers`` and the per-phase ``build_*_prompt`` functions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from llm import Prompter

from .workspace import k8s_workspace_root, resolve_k8s_experiment_id

CONVERSATION_FILENAME = "conversation.json"

# In-process cache so every phase within one run reuses the *same* Prompter
# object (and thus the live, growing history) for a given sample+experiment.
_SESSIONS: dict[tuple[str, str], Prompter] = {}


def conversation_path(sample_dir: Path) -> Path:
    """``<sampleN>/k8s-experiments/<slug>/conversation.json``."""
    return k8s_workspace_root(sample_dir) / CONVERSATION_FILENAME


def _build_conversational_prompter(
    task: Any, sample: int, vllm_port: int
) -> Prompter:
    prompter = Prompter(
        env=task.env,
        scenario=task.scenario,
        model=task.model,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        batch_size=1,
        offset=sample,
        temperature=task.temperature,
        reasoning_effort=task.reasoning_effort,
        vllm_port=vllm_port,
        provider=task.provider,
        use_stubs=task.use_stubs,
    )
    prompter.conversational = True
    return prompter


def _load_history_from_disk(
    prompter: Prompter, sample_dir: Path, logger: logging.Logger | None
) -> None:
    path = conversation_path(sample_dir)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if logger:
            logger.warning("could not load conversation history from %s: %s", path, exc)
        return
    raw_history = data.get("history") or []
    history: list[dict[str, str]] = []
    for turn in raw_history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role in {"user", "assistant"} and content is not None:
            history.append({"role": str(role), "content": str(content)})
    prompter.history = history
    if logger:
        logger.info(
            "loaded conversation history (%d turns) from %s", len(history), path
        )


def get_experiment_session(
    task: Any,
    sample_dir: Path,
    sample: int,
    *,
    vllm_port: int = 8000,
    logger: logging.Logger | None = None,
) -> Prompter:
    """
    Return the shared conversational ``Prompter`` for this sample+experiment.

    Built once per process and cached; on first build, any persisted history
    from a prior run is reloaded so the conversation continues seamlessly.
    """
    experiment_id = resolve_k8s_experiment_id()
    key = (str(sample_dir.resolve()), experiment_id)
    cached = _SESSIONS.get(key)
    if cached is not None:
        return cached
    prompter = _build_conversational_prompter(task, sample, vllm_port)
    # Stable per-conversation key so every call in this experiment routes to the
    # same OpenAI prompt-cache shard (improves hit rate; ignored by other
    # providers).
    prompter.cache_key = f"{experiment_id}:s{sample}"
    _load_history_from_disk(prompter, sample_dir, logger)
    _SESSIONS[key] = prompter
    return prompter


def persist_session(
    prompter: Prompter,
    sample_dir: Path,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Write the conversation history to ``conversation.json`` (best effort)."""
    path = conversation_path(sample_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "experiment": resolve_k8s_experiment_id(),
            "num_turns": len(prompter.history),
            "history": prompter.history,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        if logger:
            logger.warning("could not persist conversation history to %s: %s", path, exc)


def reset_session_cache() -> None:
    """Drop the in-process session cache (used by tests)."""
    _SESSIONS.clear()

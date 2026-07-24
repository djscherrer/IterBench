"""Conversation history helpers for :class:`Prompter`."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .prompter import Prompter

ChatTurn = dict[str, str]


def chat_turns(prompter: Prompter) -> list[ChatTurn]:
    """User/assistant turns to send (excludes the system prompt)."""
    if prompter.conversational and prompter.history:
        return [dict(turn) for turn in prompter.history]
    return [{"role": "user", "content": prompter.prompt}]


def append_user(prompter: Prompter, content: str) -> None:
    prompter.history.append({"role": "user", "content": content})


def append_assistant(prompter: Prompter, content: str) -> None:
    prompter.history.append({"role": "assistant", "content": content})


def send(prompter: Prompter, content: str, logger: logging.Logger) -> str:
    """
    Append a user turn, query the model with the full conversation, append the
    assistant reply, and return it. Rolls back the user turn on failure.
    """
    if not prompter.conversational:
        raise RuntimeError(
            "Prompter.send() requires conversational=True; use a session "
            "from k8s_bench.session.get_experiment_session()"
        )
    append_user(prompter, content)
    try:
        responses = prompter.prompt_model(logger)
    except Exception:
        prompter.history.pop()
        raise
    if not responses:
        prompter.history.pop()
        raise RuntimeError("LLM returned no completion")
    reply = responses[0]
    append_assistant(prompter, reply)
    return reply


def send_with_retries(
    prompter: Prompter,
    content: str,
    logger: logging.Logger,
    *,
    max_retries: int,
    base_delay: float = 1.0,
    max_delay: float = 128.0,
    log_label: str = "LLM",
) -> str:
    """Like :func:`send`, but retry transient provider failures before giving up."""
    retries = 0
    while True:
        try:
            return send(prompter, content, logger)
        except Exception as exc:
            retries += 1
            if retries > max_retries:
                logger.error("%s call failed after retries: %s", log_label, exc)
                raise
            delay = min(base_delay * 2**retries, max_delay)
            delay = random.uniform(0, delay)
            logger.warning(
                "%s attempt %d/%d failed: %s; retry in %.1fs",
                log_label,
                retries,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

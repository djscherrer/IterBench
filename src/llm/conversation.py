"""Conversation history helpers for :class:`Prompter`."""

from __future__ import annotations

import logging
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

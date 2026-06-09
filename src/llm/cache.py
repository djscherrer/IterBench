"""Prompt-cache wire formatting for conversational runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .conversation import chat_turns

if TYPE_CHECKING:
    from .prompter import Prompter

# 1h so the prefix survives multi-minute benchmark gaps (only options: 5m, 1h).
ANTHROPIC_CACHE_TTL = "1h"


def anthropic_system_param(prompter: Prompter) -> Any:
    """
    System prompt for the Anthropic call.

    In conversation mode it is returned as a typed text block carrying a
    ``cache_control`` breakpoint; otherwise it stays a plain string.
    """
    if not prompter.conversational:
        return prompter.system_prompt
    return [
        {
            "type": "text",
            "text": prompter.system_prompt,
            "cache_control": {
                "type": "ephemeral",
                "ttl": ANTHROPIC_CACHE_TTL,
            },
        }
    ]


def anthropic_messages(prompter: Prompter) -> list[dict[str, Any]]:
    """
    Anthropic ``messages`` payload with byte-stable typed blocks in
    conversational mode (rolling ``cache_control`` on the last block only).
    """
    turns = chat_turns(prompter)
    if not prompter.conversational or not turns:
        return turns
    last_idx = len(turns) - 1
    out: list[dict[str, Any]] = []
    for i, turn in enumerate(turns):
        block: dict[str, Any] = {"type": "text", "text": turn["content"]}
        if i == last_idx:
            block["cache_control"] = {
                "type": "ephemeral",
                "ttl": ANTHROPIC_CACHE_TTL,
            }
        out.append({"role": turn["role"], "content": [block]})
    return out


def openai_cache_kwargs(prompter: Prompter) -> dict[str, Any]:
    """OpenAI 24h prompt-cache retention for conversational runs."""
    if not (prompter.conversational and prompter.provider == "openai"):
        return {}
    kwargs: dict[str, Any] = {"prompt_cache_retention": "24h"}
    if prompter.cache_key:
        kwargs["prompt_cache_key"] = prompter.cache_key
    return kwargs

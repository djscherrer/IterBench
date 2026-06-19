"""Prompt-cache wire formatting for conversational runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .conversation import chat_turns

if TYPE_CHECKING:
    from .prompter import Prompter

# 1h so the prefix survives multi-minute benchmark gaps (only options: 5m, 1h).
# IMPORTANT: the cache-write price depends on this TTL (1h = 2× input, 5m =
# 1.25× input). If you change this value, update the Anthropic ``cache_write``
# rates in MODEL_PRICING (llm/usage.py) to match. The two are coupled.
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
    """
    Native-OpenAI prompt-cache request kwargs for conversational runs.

    Strictly scoped to ``provider == "openai"``: ``prompt_cache_retention`` and
    ``prompt_cache_key`` are OpenAI-specific fields. Other OpenAI-*protocol*
    providers (OpenRouter, vLLM, SwissAI) must NOT be routed through here — they
    have their own cache helpers so this function is never applied to them.
    """
    if not (prompter.conversational and prompter.provider == "openai"):
        return {}
    kwargs: dict[str, Any] = {"prompt_cache_retention": "24h"}
    if prompter.cache_key:
        kwargs["prompt_cache_key"] = prompter.cache_key
    return kwargs


def openrouter_cache_kwargs(prompter: Prompter) -> dict[str, Any]:
    """
    Prompt-cache request kwargs for OpenRouter conversational runs.

    For the models we route through OpenRouter (DeepSeek, etc.) caching is
    *implicit*: the upstream provider caches a shared prompt prefix on its own
    and OpenRouter pins follow-up calls to the same endpoint via provider sticky
    routing. Caching is therefore driven entirely by the byte-stable,
    append-only prefix that conversational mode already produces (see
    llm/messages.py) — there are no per-request parameters to send.

    Importantly, the OpenAI-specific ``prompt_cache_key`` /
    ``prompt_cache_retention`` fields are NOT accepted for these models, so we
    deliberately do not reuse :func:`openai_cache_kwargs` here. This hook exists
    so the OpenRouter path owns its own cache policy and can later grow
    model-specific behaviour (e.g. Anthropic-style ``cache_control``
    breakpoints) without reaching into the OpenAI helpers.
    """
    if not (prompter.conversational and prompter.provider == "openrouter"):
        return {}
    return {}

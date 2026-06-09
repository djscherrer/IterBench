"""Build provider ``messages`` payloads from a :class:`Prompter` instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .conversation import chat_turns

if TYPE_CHECKING:
    from .prompter import Prompter


def system_role(prompter: Prompter) -> str | None:
    """
    Role under which the system prompt is embedded in the ``messages`` list,
    or ``None`` when it is sent out-of-band (anthropic) / omitted (o1-mini).
    """
    if prompter.provider == "anthropic":
        return None
    if getattr(prompter, "openai_reasoning", False):
        return "developer"
    if prompter.model == "o1-mini":
        return None
    return "system"


def provider_messages(prompter: Prompter) -> list[dict[str, str]]:
    """
    The exact value passed as ``messages=`` to OpenAI-style providers.

    Single source of truth shared by every provider method and the outgoing-
    prompt dump. Append-only across a conversation so the provider-side prefix
    cache stays valid.
    """
    turns = chat_turns(prompter)
    role = system_role(prompter)
    if role is None:
        return turns
    return [{"role": role, "content": prompter.system_prompt}, *turns]

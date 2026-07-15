"""Completion token budgeting for chat providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prompter import Prompter


def estimate_tokens(text: str | None) -> int:
    """Rough chars→tokens estimate (~4 chars per token for English+code)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def completion_token_budget(
    prompter: Prompter,
    *,
    context_lengths: dict[str, int],
    default_cap: int = 8192,
    hard_cap: int = 65536,
    slack: int = 1024,
) -> int:
    """
    Compute a safe ``max_tokens`` for the next chat completion.

    Subtracts the actual measured prompt size so a large refinement prompt
    no longer triggers ``input + max_tokens > context_window`` errors.

    When ``prompter.model`` is absent from ``context_lengths``, returns
    ``default_cap`` (historically 8192) — models must be listed with their
    advertised context window so large codegen completions are not
    truncated early.
    """
    context = context_lengths.get(prompter.model)
    if context is None:
        return default_cap
    prompt_tokens = estimate_tokens(prompter.system_prompt)
    if prompter.conversational and prompter.history:
        for turn in prompter.history:
            prompt_tokens += estimate_tokens(turn.get("content", ""))
    else:
        prompt_tokens += estimate_tokens(prompter.prompt)
    remaining = context - prompt_tokens - slack
    if remaining <= 0:
        raise ValueError(
            f"Prompt is too large for model {prompter.model}: "
            f"~{prompt_tokens} prompt tokens + {slack} slack "
            f">= context window {context}. Trim the refinement prompt "
            f"(BAXBENCH_K8S_CODE_REFINE_MAX_CHARS) or use a larger model."
        )
    return max(512, min(hard_cap, remaining))

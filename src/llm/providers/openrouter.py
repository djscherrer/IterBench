"""OpenRouter provider adapter."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from openai import OpenAI

from ..config import OPENAI_TOGETHER_CONTEXT_LENGTHS, OPENROUTER_REMAP
from ..keys import KeyLocs
from ._base import log_inference_provider, single_completion

if TYPE_CHECKING:
    from ..prompter import Prompter

# Per-model provider routing overrides (some upstreams misbehave for a model).
_PROVIDER_IGNORE: dict[str, list[str]] = {
    "qwen/qwq-32b": ["Groq"],
    "google/gemma-3-27b-it": ["DeepInfra", "InferenceNet", "Kluster"],
    "meta-llama/llama-4-scout": ["DeepInfra", "Groq"],
    "deepseek/deepseek-chat-v3-0324": ["DeepSeek"],
}


def prompt_openrouter(prompter: Prompter, logger: logging.Logger) -> list[str]:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ[KeyLocs.openrouter_key.value],
    )
    open_router_model = OPENROUTER_REMAP.get(prompter.model, prompter.model)

    extra_kwargs: dict[str, Any] = {}
    ignore = _PROVIDER_IGNORE.get(prompter.model)
    if ignore is not None:
        extra_kwargs["extra_body"] = {"provider": {"ignore": ignore}}
    else:
        extra_kwargs["extra_body"] = None
    if prompter.model == "x-ai/grok-3-mini-beta":
        extra_kwargs["reasoning_effort"] = "high"

    response, content = single_completion(
        prompter,
        logger,
        client=client,
        provider_label="openrouter",
        model=open_router_model,
        context_lengths=OPENAI_TOGETHER_CONTEXT_LENGTHS,
        extra_kwargs=extra_kwargs,
    )
    log_inference_provider(response, logger)
    return [content]

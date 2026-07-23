"""Shared helpers for OpenAI-protocol provider adapters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openai import NOT_GIVEN, OpenAI

from ..cache import openai_cache_kwargs
from ..config import OPENAI_MAX_COMPLETION_TOKENS, OPENAI_TOGETHER_CONTEXT_LENGTHS
from ..messages import provider_messages
from ..token_budget import completion_token_budget
from ..usage import usage_from_openai_style

if TYPE_CHECKING:
    from ..prompter import Prompter


def store_openai_usage(prompter: Prompter, usage: Any, *, provider: str) -> None:
    prompter.last_usage = usage_from_openai_style(
        usage, model=prompter.model, provider=provider
    )


def extract_content(response: Any, logger: logging.Logger) -> str:
    if response.choices is None:
        logger.error("Response was None: %s", response)
        raise Exception("No content")
    content = response.choices[0].message.content
    if content is None or len(content) == 0:
        raise Exception("No content")
    return content


def log_token_stats(response: Any, logger: logging.Logger) -> None:
    if response.usage is not None:
        logger.info(
            "Token stats: %s; around %s completion tokens per completion",
            response.usage,
            response.usage.completion_tokens,
        )
    else:
        logger.info("Token stats unavailable")


def log_inference_provider(response: Any, logger: logging.Logger) -> None:
    try:
        logger.info("Inference provided by: %s", response.provider)
        logger.info("Inference id: %s", response.id)
    except Exception:
        pass


def single_completion(
    prompter: Prompter,
    logger: logging.Logger,
    *,
    client: OpenAI,
    provider_label: str,
    model: str | None = None,
    context_lengths: dict[str, int] = OPENAI_TOGETHER_CONTEXT_LENGTHS,
    extra_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Run a single (``n=1``) OpenAI-style chat completion and store usage."""
    response = client.chat.completions.create(
        model=model or prompter.model,
        messages=provider_messages(prompter),
        n=1,
        temperature=prompter.temperature,
        max_tokens=completion_token_budget(
            prompter,
            context_lengths=context_lengths,
        ),
        **(extra_kwargs or {}),
    )
    content = extract_content(response, logger)
    store_openai_usage(prompter, response.usage, provider=provider_label)
    log_token_stats(response, logger)
    if response.choices[0].finish_reason == "length":
        logger.warning("Completion was cut off due to length.")
    return response, content


def batch_completion(
    prompter: Prompter,
    logger: logging.Logger,
    *,
    client: OpenAI,
    provider_label: str,
) -> list[str]:
    """Run a batched (``n=batch_size``) OpenAI-style completion (openai/together)."""
    extra_kwargs: dict[str, Any] = {}
    if prompter.openai_reasoning:
        extra_kwargs["reasoning_effort"] = prompter.reasoning_effort
    if prompter.provider == "openai":
        if prompter.model in OPENAI_MAX_COMPLETION_TOKENS:
            extra_kwargs["max_completion_tokens"] = OPENAI_MAX_COMPLETION_TOKENS[
                prompter.model
            ]
        else:
            # dict.get(key, default) would evaluate this fallback eagerly on
            # every call regardless of whether `key` is present, so it must
            # stay behind an explicit branch rather than folded into .get().
            extra_kwargs["max_completion_tokens"] = completion_token_budget(
                prompter,
                context_lengths=OPENAI_TOGETHER_CONTEXT_LENGTHS,
                hard_cap=128000,
            )
    else:
        extra_kwargs["max_tokens"] = completion_token_budget(
            prompter,
            context_lengths=OPENAI_TOGETHER_CONTEXT_LENGTHS,
        )
    extra_kwargs.update(openai_cache_kwargs(prompter))

    completions = client.chat.completions.create(
        model=prompter.model,
        messages=provider_messages(prompter),
        n=prompter.batch_size,
        temperature=(
            prompter.temperature if not prompter.openai_reasoning else NOT_GIVEN
        ),
        **extra_kwargs,
    )
    if completions.usage is not None:
        store_openai_usage(prompter, completions.usage, provider=provider_label)
        logger.info(
            "Batch token stats: %s; around %.2f completion tokens per completion",
            completions.usage,
            completions.usage.completion_tokens / prompter.batch_size,
        )
    else:
        logger.info("Batch token stats unavailable")

    responses: list[str] = []
    for idx, choice in enumerate(completions.choices):
        if choice.finish_reason == "length":
            logger.warning("Completion %d was cut off due to length.", idx)
        if choice.message.content:
            responses.append(choice.message.content)
    return responses

"""Anthropic Messages API adapter."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from anthropic import Anthropic
from anthropic.types import TextBlock

from ..cache import anthropic_messages, anthropic_system_param
from ..config import ANTHROPIC_THINKING_LENGTHS
from ..keys import KeyLocs
from ..usage import usage_from_anthropic

if TYPE_CHECKING:
    from ..prompter import Prompter


def prompt_anthropic(prompter: Prompter, logger: logging.Logger) -> list[str]:
    client = Anthropic(api_key=os.environ[KeyLocs.anthropic_key.value])
    if prompter.anthropic_thinking:
        text, thinking = "", ""
        thinking_extra: dict[str, Any] = {}
        if prompter.conversational:
            thinking_extra["system"] = anthropic_system_param(prompter)
        with client.messages.stream(
            model=prompter.model,
            thinking={"type": "adaptive"},
            messages=anthropic_messages(prompter),
            max_tokens=ANTHROPIC_THINKING_LENGTHS[prompter.model],
            **thinking_extra,
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        thinking += event.delta.thinking
                    elif event.delta.type == "text_delta":
                        text += event.delta.text
            final = stream.get_final_message()
            prompter.last_usage = usage_from_anthropic(final.usage, model=prompter.model)
        logger.info("Thinking traces:\n %s", thinking)
        if final.stop_reason == "max_tokens":
            logger.warning("Completion was cut off due to length.")
        return [text]

    response = client.messages.create(
        model=prompter.model,
        system=anthropic_system_param(prompter),
        messages=anthropic_messages(prompter),
        temperature=prompter.temperature,
        max_tokens=8192 if "claude-3-5-" in prompter.model else 4096,
    )
    assert isinstance(response.content[0], TextBlock)
    prompter.last_usage = usage_from_anthropic(response.usage, model=prompter.model)
    if response.usage is not None:
        logger.info(
            "Token stats: %s; around %s completion tokens per completion",
            response.usage,
            response.usage.output_tokens,
        )
    if response.stop_reason == "max_tokens":
        logger.warning("Completion was cut off due to length.")
    return [response.content[0].text]

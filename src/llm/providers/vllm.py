"""Local vLLM server provider adapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from openai import OpenAI

from ..config import VLLM_CONTEXT_LENGTHS
from ._base import log_inference_provider, single_completion

if TYPE_CHECKING:
    from ..prompter import Prompter

_GPT_OSS_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")


def prompt_vllm(prompter: Prompter, logger: logging.Logger) -> list[str]:
    long_timeout_client = httpx.Client(timeout=18000)
    client = OpenAI(
        base_url=f"http://localhost:{prompter.vllm_port}/v1",
        api_key="EMPTY",
        http_client=long_timeout_client,
    )
    extra_kwargs: dict[str, Any] = {}
    if prompter.model in _GPT_OSS_MODELS:
        extra_kwargs["reasoning_effort"] = prompter.reasoning_effort

    response, content = single_completion(
        prompter,
        logger,
        client=client,
        provider_label="vllm",
        context_lengths=VLLM_CONTEXT_LENGTHS,
        default_cap=8192,
        extra_kwargs=extra_kwargs,
    )
    if prompter.model in _GPT_OSS_MODELS:
        reasoning_content = response.choices[0].message.reasoning_content
        logger.info(
            "Reasoning traces:\n---BEGINNING OF REASONING---\n%s\n---END OF REASONING---",
            reasoning_content,
        )
    log_inference_provider(response, logger)
    return [content]

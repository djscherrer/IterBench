import logging
import os
import pathlib
import random
import re
import time
import traceback
from enum import Enum
from typing import Any, cast, no_type_check
from venv import logger

import httpx
from anthropic import Anthropic
from anthropic.types import TextBlock
from openai import NOT_GIVEN, OpenAI, api_key
from openai.types.chat import ChatCompletionMessageParam

from env.base import Env
from llm_usage import TokenUsage, usage_from_anthropic, usage_from_openai_style
from scenarios.base import Scenario

_SYSTEM_PROMPT = "You are an experienced full-stack developer"


class KeyLocs(Enum):
    openai_key = "OPENAI_API_KEY"
    anthropic_key = "ANTHROPIC_API_KEY"
    together_key = "TOGETHER_API_KEY"
    openrouter_key = "OPENROUTER_API_KEY"
    cscs_key = "CSCS_API_KEY"


class Prompter:

    # NOTE: unused because Together expects you to set
    # max_tokens=context_length-numTokens(prompt)
    # so we hardcode below for now
    openai_together_context_lengths = {
        "mistralai/Mixtral-8x22B-Instruct-v0.1": 65536,
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": 131072,
        "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free": 8100,
        "deepseek-ai/DeepSeek-V3": 131072,
        "Qwen/Qwen2.5-Coder-32B-Instruct": 32768,
        "Qwen/Qwen2.5-72B-Instruct-Turbo": 32768,
        "Qwen/Qwen2.5-7B-Instruct-Turbo": 32768,
        "Qwen/Qwen3-Next-80B-A3B-Thinking": 32768,
        "gpt-4o": 128000,
        "chatgpt-4o-latest": 128000,
        "gpt-4.1-2025-04-14": 32000,
        "gpt-4.1-mini-2025-04-14": 32000,
        "o1": 200000,
        "o1-mini": 128000,
        "o3-mini": 200000,
        "deepseek-ai/DeepSeek-R1": 164000,
        "google/gemma-2-27b-it": 8192,
        "deepseek-ai/DeepSeek-R1-Distill-Llama-70B": 131072,
        "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B": 131072,
        "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B": 131072,
        "Qwen/QwQ-32B": 32768,
        "qwen/qwq-32b": 128000,
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": 524288,
        "meta-llama/Llama-4-Scout-17B-16E-Instruct": 327680,
        "google/gemini-2.5-pro-preview-03-25": 65000,
        "mistralai/mistral-small-3.1-24b-instruct": 33000,
        "google/gemma-3-27b-it": 32000,
        "meta-llama/llama-4-scout": 32000,
        "deepseek/deepseek-chat-v3-0324": 16000,
        "mistral/ministral-8b": 128000,
        "x-ai/grok-3-beta": 128000,
        "x-ai/grok-3-mini-beta": 128000,
        "Qwen/Qwen3-235B-A22B-fp8-tput": 40000,
        "qwen/qwen3-235b-a22b": 40000,
        "deepseek/deepseek-r1-0528": 32000,
        "x-ai/grok-4": 256000,
        "qwen/qwen3-coder": 200000,
        "openai/gpt-5": 256000,
        "deepseek/deepseek-v3.2": 160000,
        "gpt-5.4-nano": 128000,
    }

    anthropic_thinking_lengths = {
        "claude-opus-4-20250514": 32000,
        "claude-sonnet-4-20250514": 64000,
        "claude-3-7-sonnet-20250219": 64000,
        "claude-opus-4-1-20250805": 32000,
        "claude-opus-4-7": 128000,
        "claude-opus-4-6": 128000,
    }

    vllm_context_lengths = {
        "sri-blaze/kodcode -v1": 32000,
        "eth-sri/kodcode-v1-qwq-3-48-1e-5": 32000,
        "agentica-org/DeepCoder-14B-Preview": 32000,
        "eth-sri/kodcode-v1-multi-clear-qwq-3-48-1e-5": 32000,
        "eth-sri/qwq-cybernative3k_kodcodeV1": 32000,
        "eth-sri/qwq-cybernative3k_snyk_kodcodeV1": 32000,
        "eth-sri/deepseek-r1-distill-qwen-7b-cybernative-snyk-kodcodeV1": 32000,
        "openai/gpt-oss-120b": 16384,
        "openai/gpt-oss-20b": 16384,
    }

    openai_max_completion_tokens = {
        "gpt-4o": 16384,
        "chatgpt-4o-latest": 16384,
        "o1": 100000,
        "o1-mini": 65536,
        "o3-mini": 100000,
        "gpt-4.1-2025-04-14": 32000,
        "gpt-4.1-mini-2025-04-14": 32000,
        "o3-2025-04-16": 100000,
        "o4-mini-2025-04-16": 100000,
        "gpt-5-2025-08-07": 128000,
        "gpt-5.4": 128000,
        "gpt-5.4-2026-03-05": 128000,
        "gpt-5.4-nano": 128000,
    }

    openrouter_remap = {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": "meta-llama/llama-3.3-70b-instruct",
        "deepseek-ai/DeepSeek-V3": "deepseek/deepseek-chat",
        "Qwen/Qwen2.5-Coder-32B-Instruct": "qwen/qwen-2.5-coder-32b-instruct",
        "Qwen/Qwen2.5-7B-Instruct-Turbo": "qwen/qwen-2.5-7b-instruct",
        "Qwen/Qwen2.5-72B-Instruct-Turbo": "qwen/qwen-2.5-72b-instruct",
        "Qwen/Qwen3-235B-A22B-fp8-tput": "qwen/qwen3-235b-a22b",
    }

    def __init__(
        self,
        env: Env,
        scenario: Scenario,
        model: str,
        spec_type: str,
        safety_prompt: str,
        batch_size: int,
        offset: int,
        temperature: float,
        reasoning_effort: str,
        vllm_port: int,
        provider: str | None,
        use_stubs: bool = True,
    ):
        self.env = env
        self.scenario = scenario
        self.spec_type = spec_type
        self.safety_prompt = safety_prompt
        self.model = model
        self.batch_size = batch_size
        self.offset = offset
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.vllm_port = vllm_port
        self.use_stubs = use_stubs

        self.system_prompt = _SYSTEM_PROMPT

        _NATIVE_PROVIDER_PREFIXES = (
            "anthropic",
            "openrouter",
            "together_ai",
            "swissai",
            "openai",
        )
        if provider is None:
            provider_prefix = self.model.split("/")[0]
            if provider_prefix in _NATIVE_PROVIDER_PREFIXES:
                self.provider = provider_prefix
                self.model = self.model[len(provider_prefix) + 1 :]
            else:
                raise ValueError(f"Cannot infer provider from model name: {self.model}, please specify provider explicitly or use a known prefixed provider.")
        else:
            self.provider = provider
            # Defensive: when the caller passes ``model="anthropic/claude-…"``
            # *and* ``provider="anthropic"`` (instead of leaving provider=None),
            # the native Anthropic SDK gets called with a literal model id that
            # contains ``anthropic/`` and returns 404. Strip the matching
            # prefix here so the two ways of specifying the model converge.
            # Routing providers (openrouter, together_ai, swissai, vllm) still
            # accept the ``vendor/model`` form, so we only strip when the
            # provider equals the prefix.
            head, sep, tail = self.model.partition("/")
            if sep and head == provider and head in _NATIVE_PROVIDER_PREFIXES:
                self.model = tail

        # NOTE: `self.model` may be normalized above (provider prefix stripped).
        # Any model-name based feature flags must use `self.model`, not the raw input `model`.
        self.openai_reasoning = (
            self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
            or self.model.startswith("gpt-5")
        )
        self.anthropic_thinking = self.model in self.anthropic_thinking_lengths

        self.prompt = self.scenario.build_prompt(
            self.env, self.spec_type, self.safety_prompt, agent=False, use_stubs=use_stubs
        )
        self.last_usage: TokenUsage | None = None

    def _store_openai_usage(self, usage: Any, *, provider: str) -> None:
        self.last_usage = usage_from_openai_style(
            usage, model=self.model, provider=provider
        )

    def _store_anthropic_usage(self, usage: Any) -> None:
        self.last_usage = usage_from_anthropic(usage, model=self.model)

    @staticmethod
    def _estimate_tokens(text: str | None) -> int:
        """Rough chars→tokens estimate (~4 chars per token for English+code)."""
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    def _completion_token_budget(
        self,
        *,
        context_lengths: dict[str, int],
        default_cap: int = 8192,
        hard_cap: int = 16384,
        slack: int = 1024,
    ) -> int:
        """
        Compute a safe ``max_tokens`` for the next chat completion.

        Replaces the old ``context_length - 3000`` formula that assumed the
        prompt was tiny. Subtracts the **actual** measured prompt size so a
        large refinement prompt no longer triggers ``input + max_tokens >
        context_window`` 400 errors from the provider.

        - ``context_lengths``: per-model true context window dict
          (e.g. ``openai_together_context_lengths``).
        - ``default_cap``: used when the model is missing from the dict.
        - ``hard_cap``: per-response ceiling — a single ``app.js`` rewrite
          rarely needs more than ~6k tokens; 16k leaves room for multi-file
          scenarios without burning context.
        - ``slack``: tokenizer disagreement + system overhead headroom.
        """
        context = context_lengths.get(self.model)
        if context is None:
            return default_cap
        prompt_tokens = self._estimate_tokens(self.system_prompt) + \
                        self._estimate_tokens(self.prompt)
        remaining = context - prompt_tokens - slack
        if remaining <= 0:
            raise ValueError(
                f"Prompt is too large for model {self.model}: "
                f"~{prompt_tokens} prompt tokens + {slack} slack "
                f">= context window {context}. Trim the refinement prompt "
                f"(BAXBENCH_K8S_CODE_REFINE_MAX_CHARS) or use a larger model."
            )
        return max(512, min(hard_cap, remaining))

    @no_type_check
    def prompt_anthropic(self, logger: logging.Logger) -> list[str]:
        client = Anthropic(api_key=os.environ[KeyLocs.anthropic_key.value])
        try:
            if self.anthropic_thinking:
                text, thinking = "", ""
                with client.messages.stream(
                    model=self.model,
                    thinking={
                        # To use for older anthropic models
                        # "type": "enabled", 
                        # "budget_tokens": self.anthropic_thinking_lengths[self.model] - 1,
                        "type": "adaptive",
                    },
                    messages=[
                        {"role": "user", "content": self.prompt},
                    ],
                    max_tokens=self.anthropic_thinking_lengths[self.model],
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_delta":
                            if event.delta.type == "thinking_delta":
                                thinking += event.delta.thinking
                            elif event.delta.type == "text_delta":
                                text += event.delta.text
                    final = stream.get_final_message()
                    self._store_anthropic_usage(final.usage)
                logger.info(f"Thinking traces:\n {thinking}")
                if final.stop_reason == "max_tokens":
                    logger.warning("Completion was cut off due to length.")
                return [text]
            else:
                response = client.messages.create(
                    model=self.model,
                    system=self.system_prompt,
                    messages=[
                        {"role": "user", "content": self.prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=8192 if "claude-3-5-" in self.model else 4096,
                )
                assert isinstance(response.content[0], TextBlock)
                self._store_anthropic_usage(response.usage)
                if response.usage is not None:
                    logger.info(
                        f"Token stats: {response.usage}; around {response.usage.output_tokens} completion tokens per completion"
                    )
                if response.stop_reason == "max_tokens":
                    logger.warning(f"Completion was cut off due to length.")
                return [response.content[0].text]
        except Exception as e:
            raise e

    @no_type_check
    def prompt_openrouter(self, logger: logging.Logger) -> list[str]:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ[KeyLocs.openrouter_key.value],
        )
        if self.model in self.openrouter_remap:
            open_router_model = self.openrouter_remap[self.model]
        else:
            open_router_model = self.model
        try:
            extra_body: None | Any = None
            if self.model == "qwen/qwq-32b":
                extra_body = {
                    "provider": {"ignore": ["Groq"]},
                }
            elif self.model == "google/gemma-3-27b-it":
                extra_body = {
                    "provider": {"ignore": ["DeepInfra", "InferenceNet", "Kluster"]},
                }
            elif self.model == "meta-llama/llama-4-scout":
                extra_body = {
                    "provider": {"ignore": ["DeepInfra", "Groq"]},
                }
            elif self.model == "deepseek/deepseek-chat-v3-0324":
                extra_body = {
                    "provider": {"ignore": ["DeepSeek"]},
                }
            else:
                extra_body = None
            if self.model == "x-ai/grok-3-mini-beta":
                extra_kwargs = {"reasoning_effort": "high"}
            else:
                extra_kwargs = {}
            response = client.chat.completions.create(
                model=open_router_model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self.prompt},
                ],
                n=1,
                temperature=self.temperature,
                max_tokens=self._completion_token_budget(
                    context_lengths=Prompter.openai_together_context_lengths,
                ),
                extra_body=extra_body,
                **extra_kwargs,
            )
            if response.choices is None:
                logger.error(f"Response was None: {response}")
                raise Exception("No content")
            content = response.choices[0].message.content
            if content is not None and len(content) > 0:
                self._store_openai_usage(response.usage, provider="openrouter")
                if response.usage is not None:
                    logger.info(
                        f"Token stats: {response.usage}; around {response.usage.completion_tokens} completion tokens per completion"
                    )
                else:
                    logger.info(f"Token stats unavailable")
                if response.choices[0].finish_reason == "length":
                    logger.warning(f"Completion was cut off due to length.")
                try:
                    logger.info(f"Inference provided by: {response.provider}")
                    logger.info(f"Inference id: {response.id}")
                except:
                    pass
                return [content]
            else:
                raise Exception("No content")
        except Exception as e:
            raise e

    def prompt_vllm(self, logger: logging.Logger) -> list[str]:
        long_timeout_client = httpx.Client(timeout=18000)
        client = OpenAI(
            base_url=f"http://localhost:{self.vllm_port}/v1",
            api_key="EMPTY",
            http_client=long_timeout_client,
        )
        try:
            extra_kwargs: dict[str, Any] = {}
            if (
                self.model == "openai/gpt-oss-120b"
                or self.model == "openai/gpt-oss-20b"
            ):
                extra_kwargs["reasoning_effort"] = self.reasoning_effort
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self.prompt},
                ],
                n=1,
                temperature=self.temperature,
                max_tokens=self._completion_token_budget(
                    context_lengths=Prompter.vllm_context_lengths,
                    default_cap=8192,
                ),
                **extra_kwargs,
            )
            if response.choices is None:
                logger.error(f"Response was None: {response}")
                raise Exception("No content")
            content = response.choices[0].message.content
            if (
                self.model == "openai/gpt-oss-120b"
                or self.model == "openai/gpt-oss-20b"
            ):
                reasoning_content = response.choices[0].message.reasoning_content
                logger.info(
                    f"Reasoning traces:\n---BEGINNING OF REASONING---\n{reasoning_content}\n---END OF REASONING---"
                )
            if content is not None and len(content) > 0:
                self._store_openai_usage(response.usage, provider="vllm")
                if response.usage is not None:
                    logger.info(
                        f"Token stats: {response.usage}; around {response.usage.completion_tokens} completion tokens per completion"
                    )
                else:
                    logger.info(f"Token stats unavailable")
                if response.choices[0].finish_reason == "length":
                    logger.warning(f"Completion was cut off due to length.")
                try:
                    logger.info(f"Inference provided by: {response.provider}")
                    logger.info(f"Inference id: {response.id}")
                except:
                    pass
                return [content]
            else:
                raise Exception("No content")
        except Exception as e:
            raise e

    def prompt_swissai(self, logger: logging.Logger) -> list[str]:
        client = OpenAI(
            api_key=os.environ[KeyLocs.cscs_key.value],
            base_url="https://api.swissai.cscs.ch/v1",
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self.prompt},
                ],
                n=1,
                temperature=self.temperature,
                max_tokens=self._completion_token_budget(
                    context_lengths=Prompter.openai_together_context_lengths,
                ),
            )
            if response.choices is None:
                logger.error(f"Response was None: {response}")
                raise Exception("No content")
            content = response.choices[0].message.content
            if content is not None and len(content) > 0:
                self._store_openai_usage(response.usage, provider="swissai")
                if response.usage is not None:
                    logger.info(
                        f"Token stats: {response.usage}; around {response.usage.completion_tokens} completion tokens per completion"
                    )
                else:
                    logger.info(f"Token stats unavailable")
                if response.choices[0].finish_reason == "length":
                    logger.warning(f"Completion was cut off due to length.")
                return [content]
            else:
                raise Exception("No content")
        except Exception as e:
            raise e

    def prompt_openai_batch(self, logger: logging.Logger) -> list[str]:

        client = OpenAI(api_key=os.environ[KeyLocs.openai_key.value])

        try:
            # Prepare extra kwargs
            extra_kwargs: dict[str, Any] = {}
            if self.openai_reasoning: 
                extra_kwargs["reasoning_effort"] = self.reasoning_effort
            if self.provider == "openai":
                extra_kwargs["max_completion_tokens"] = (
                    Prompter.openai_max_completion_tokens[self.model]
                )
            else:
                extra_kwargs["max_tokens"] = self._completion_token_budget(
                    context_lengths=Prompter.openai_together_context_lengths,
                )
            # Prepare the message
            messages: list[Any] = []
            if self.openai_reasoning:
                messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {"role": "developer", "content": self.system_prompt},
                    )
                )
            elif self.model == "o1-mini":
                # No sysprompt
                pass
            else:
                messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {"role": "system", "content": self.system_prompt},
                    )
                )
            messages.append({"role": "user", "content": self.prompt})

            # Query
            completions = client.chat.completions.create(
                model=self.model,
                messages=messages,
                n=self.batch_size,
                temperature=(
                    self.temperature if not self.openai_reasoning else NOT_GIVEN
                ),
                **extra_kwargs,
            )
            if completions.usage is not None:
                self._store_openai_usage(completions.usage, provider="openai")
                logger.info(
                    f"Batch token stats: {completions.usage}; around {completions.usage.completion_tokens / self.batch_size:.2f} completion tokens per completion"
                )
            else:
                logger.info(f"Batch token stats unavailable")
            responses = []
            for idx, choice in enumerate(completions.choices):
                if choice.finish_reason == "length":
                    logger.warning(f"Completion {idx} was cut off due to length.")
                if choice.message.content:
                    responses.append(choice.message.content)
            return responses

        except Exception as e:
            raise e
        
    def prompt_togetherai_batch(self, logger: logging.Logger) -> list[str]:

        client = OpenAI(
            api_key=os.environ[KeyLocs.together_key.value],
            base_url="https://api.together.xyz/v1",
        )

        try:
            # Prepare extra kwargs
            extra_kwargs: dict[str, Any] = {}
            if self.openai_reasoning: 
                extra_kwargs["reasoning_effort"] = self.reasoning_effort
            if self.provider == "openai":
                extra_kwargs["max_completion_tokens"] = (
                    Prompter.openai_max_completion_tokens[self.model]
                )
            else:
                extra_kwargs["max_tokens"] = self._completion_token_budget(
                    context_lengths=Prompter.openai_together_context_lengths,
                )
            # Prepare the message
            messages: list[Any] = []
            if self.openai_reasoning:
                messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {"role": "developer", "content": self.system_prompt},
                    )
                )
            elif self.model == "o1-mini":
                # No sysprompt
                pass
            else:
                messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {"role": "system", "content": self.system_prompt},
                    )
                )
            messages.append({"role": "user", "content": self.prompt})

            # Query
            completions = client.chat.completions.create(
                model=self.model,
                messages=messages,
                n=self.batch_size,
                temperature=(
                    self.temperature if not self.openai_reasoning else NOT_GIVEN
                ),
                **extra_kwargs,
            )
            if completions.usage is not None:
                self._store_openai_usage(completions.usage, provider="together_ai")
                logger.info(
                    f"Batch token stats: {completions.usage}; around {completions.usage.completion_tokens / self.batch_size:.2f} completion tokens per completion"
                )
            else:
                logger.info(f"Batch token stats unavailable")
            responses = []
            for idx, choice in enumerate(completions.choices):
                if choice.finish_reason == "length":
                    logger.warning(f"Completion {idx} was cut off due to length.")
                if choice.message.content:
                    responses.append(choice.message.content)
            return responses

        except Exception as e:
            raise e

    @no_type_check
    def prompt_model(self, logger: logging.Logger) -> list[str]:
        self.last_usage = None
        if self.provider == "anthropic":
            responses = self.prompt_anthropic(logger)
        elif self.provider =="openrouter":
            responses = self.prompt_openrouter(logger)
        elif self.provider == "vllm":
            responses = self.prompt_vllm(logger)
        elif self.provider == "swissai":
            responses = self.prompt_swissai(logger)
        elif self.provider == "openai":
            responses = self.prompt_openai_batch(logger)
        elif self.provider == "together_ai":
            responses = self.prompt_togetherai_batch(logger)
        else:
            logger.error(f"Unknown provider: {self.provider}")
            raise Exception(f"Unknown provider: {self.provider}")
        if self.last_usage is not None:
            logger.info(
                "Estimated LLM cost: $%.4f (%d in + %d out tokens, model=%s)",
                self.last_usage.estimated_cost_usd,
                self.last_usage.input_tokens,
                self.last_usage.output_tokens,
                self.last_usage.model,
            )
        return responses

    def get_code_dir(self, save_dir: pathlib.Path, sample: int) -> pathlib.Path:
        return save_dir / f"sample{sample}" / "code"

    def save_code(
        self, files: dict[pathlib.Path, str], results_dir: pathlib.Path, sample: int
    ) -> None:
        code_dir = self.get_code_dir(results_dir, sample)
        code_dir.mkdir(parents=True, exist_ok=True)
        for path, code in files.items():
            full_path = code_dir / path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w") as f:
                f.write(code)

    def prompt_model_batch_with_exp_backoff(
        self,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        save_dir: pathlib.Path,
        logger: logging.Logger,
        *,
        cost_workspace: pathlib.Path | None = None,
        cost_call_type: str = "scenario_code_generation",
    ) -> None:
        # Anthropic, OpenRouter, SwissAI, and VLLM don't support batching, so we have to sample a single completion multiple times
        n_times_to_sample = (
            self.batch_size 
            if self.provider in ["swissai", "anthropic", "openrouter", "vllm"]
            else 1
        )
        for i in range(n_times_to_sample):
            retries = 0
            while True:
                try:
                    if retries > 0:
                        logger.info(f"Retrying {retries} times")
                    completion = self.prompt_model(logger)
                    if cost_workspace is not None:
                        from llm_usage import enforce_cost_budget, record_prompter_usage

                        enforce_cost_budget(cost_workspace)
                        record_prompter_usage(
                            prompter=self,
                            call_type=cost_call_type,
                            workspace=cost_workspace,
                            logger=logger,
                            artifact_dir=save_dir,
                            note=f"sample_offset={i + self.offset} n={n_times_to_sample}",
                        )
                    raw_comps = (
                        "\n\n<<<RESPONSE DELIM>>>\n\n".join(completion)
                        + "\n\n<<<RESPONSE DELIM>>>\n\n"
                    )
                    logger.info(
                        f"Got {len(completion) + i + self.offset}/{self.batch_size + self.offset} responses. Parsing and saving. Raw responses:\n\n{raw_comps}"
                    )
                    file_contents = [
                        Parser(self.env, logger).parse_response(c) for c in completion
                    ]
                    for j, files in enumerate(file_contents):
                        try:
                            self.save_code(files, save_dir, i + j + self.offset)
                            logger.info("saved code sample %d", i + j + self.offset)
                        except Exception as e:
                            logger.exception("got exception:\n%s", str(e), exc_info=e)
                        logger.info("-" * 80)

                    break
                except Exception as e:
                    retries += 1
                    if retries > max_retries:
                        logger.error(f"Max retries reached, raising exception: {e}")
                        raise e
                    delay = min(base_delay * 2**retries, max_delay)
                    delay = random.uniform(0, delay)
                    logger.exception(
                        f"{e}, backing off for {delay} seconds", exc_info=e
                    )
                    time.sleep(delay)


class Parser:

    def __init__(self, env: Env, logger: logging.Logger):
        self.env = env
        self.logger = logger

        self.fp_pattern = re.compile(r"<FILEPATH>(.+?)</FILEPATH>", re.DOTALL)
        self.fp_ht_pattern = re.compile(r"^###\s*(.+?)$", re.DOTALL | re.MULTILINE)
        self.md_pattern = re.compile(r"```(?!bash)\w+\n(.*?)\n```", re.DOTALL)
        self.code_pattern = re.compile(r"<CODE>(.+?)</CODE>", re.DOTALL)

    def _invalid(self, response: str) -> dict[pathlib.Path, str]:
        self.logger.warning(f"Format not found")
        return {pathlib.Path("failed"): "Format not found. Full response:\n" + response}

    def _clean(self, s: str) -> str:
        s = s.strip()
        if s.startswith("**"):
            s = s[2:]
        if s.endswith("**"):
            s = s[:-2]
        s = s.strip()
        return s

    def _parse_md(self, response: str) -> list[str]:
        return [self._clean(s) for s in self.md_pattern.findall(response)]

    def _parse_code(self, response: str) -> list[str]:
        return [self._clean(s) for s in self.code_pattern.findall(response)]

    def _parse_multi_file_response(self, response: str) -> dict[pathlib.Path, str]:
        normal_file_paths = [
            pathlib.Path(self._clean(s)) for s in self.fp_pattern.findall(response)
        ]
        # NOTE: asserts that these patterns 1) are not mixed with normal filepaths 2) are not mixed with titles
        ht_file_paths = [
            pathlib.Path(self._clean(s)) for s in self.fp_ht_pattern.findall(response)
        ]
        for file_paths in (
            normal_file_paths,
            ht_file_paths,
        ):
            code_snippets_md = self._parse_md(response)
            code_snippets_code = self._parse_code(response)
            self.logger.info(f"Trying MD parsing")
            if len(file_paths) == len(code_snippets_md) and len(file_paths) > 0:
                return {fp: c for fp, c in zip(file_paths, code_snippets_md)}
            elif len(file_paths) == len(code_snippets_code) and len(file_paths) > 0:
                self.logger.warning(f"MD format not found, trying CODE format")
                # failsave code parsing in case some of them have md and some not
                codes = []
                for code in code_snippets_code:
                    md_parsed = self._parse_md(code)
                    if len(md_parsed) > 0:
                        codes.append(md_parsed[0])
                    else:
                        codes.append(code)
                assert len(codes) == len(code_snippets_code)
                return {fp: c for fp, c in zip(file_paths, codes)}
        self.logger.warning(
            f"Both formats failed, lengths are: files {len(file_paths)}, md {len(code_snippets_md)}, code {len(code_snippets_code)}"
        )
        return self._invalid(response)

    def _parse_single_file_response(self, response: str) -> dict[pathlib.Path, str]:
        assert self.env.code_filename is not None
        code_snippets_md = self._parse_md(response)
        code_snippets_code = self._parse_code(response)
        self.logger.info(f"Trying MD parsing")
        if len(code_snippets_md) > 0:
            return {pathlib.Path(self.env.code_filename): code_snippets_md[0]}
        elif len(code_snippets_code) > 0:
            self.logger.warning(f"MD format not found, trying CODE format")
            return {pathlib.Path(self.env.code_filename): code_snippets_code[0]}
        else:
            self.logger.warning(f"Both formats failed")
            return self._invalid(response)

    def parse_response(self, response: str) -> dict[pathlib.Path, str]:
        if self.env.is_multi_file:
            return self._parse_multi_file_response(response)
        else:
            return self._parse_single_file_response(response)

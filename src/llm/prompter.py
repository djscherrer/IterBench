"""Core :class:`Prompter` — scenario prompts, conversation mode, provider dispatch."""

from __future__ import annotations

import logging
import pathlib
import random
import time
from typing import Any

from env.base import Env
from scenarios.base import Scenario

from . import conversation, messages
from .config import (
    ANTHROPIC_THINKING_LENGTHS,
    NATIVE_PROVIDER_PREFIXES,
    OPENAI_MAX_COMPLETION_TOKENS,
    OPENAI_TOGETHER_CONTEXT_LENGTHS,
    OPENROUTER_REMAP,
    SYSTEM_PROMPT,
    VLLM_CONTEXT_LENGTHS,
)
from .dump import dump_outgoing_prompt
from .parser import Parser
from .providers import anthropic as anthropic_provider
from .providers import openai as openai_provider
from .providers import openrouter as openrouter_provider
from .providers import swissai as swissai_provider
from .providers import together as together_provider
from .providers import vllm as vllm_provider
from .usage import TokenUsage, enforce_cost_budget, record_prompter_usage


class Prompter:
    openai_together_context_lengths = OPENAI_TOGETHER_CONTEXT_LENGTHS
    anthropic_thinking_lengths = ANTHROPIC_THINKING_LENGTHS
    vllm_context_lengths = VLLM_CONTEXT_LENGTHS
    openai_max_completion_tokens = OPENAI_MAX_COMPLETION_TOKENS
    openrouter_remap = OPENROUTER_REMAP

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

        self.system_prompt = SYSTEM_PROMPT

        if provider is None:
            provider_prefix = self.model.split("/")[0]
            if provider_prefix in NATIVE_PROVIDER_PREFIXES:
                self.provider = provider_prefix
                self.model = self.model[len(provider_prefix) + 1 :]
            else:
                raise ValueError(
                    f"Cannot infer provider from model name: {self.model}, "
                    "please specify provider explicitly or use a known prefixed provider."
                )
        else:
            self.provider = provider
            head, sep, tail = self.model.partition("/")
            if sep and head == provider and head in NATIVE_PROVIDER_PREFIXES:
                self.model = tail

        self.openai_reasoning = (
            self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
            or self.model.startswith("gpt-5")
        )
        self.anthropic_thinking = self.model in ANTHROPIC_THINKING_LENGTHS

        self.prompt = self.scenario.build_prompt(
            self.env, self.spec_type, self.safety_prompt, agent=False, use_stubs=use_stubs
        )
        self.last_usage: TokenUsage | None = None

        self.conversational: bool = False
        self.history: list[dict[str, str]] = []
        self.cache_key: str | None = None

    @classmethod
    def for_chat(
        cls,
        *,
        model: str,
        provider: str,
        system_prompt: str,
        history: list[dict[str, str]],
        temperature: float,
        reasoning_effort: int | str | None = None,
        batch_size: int = 1,
    ) -> "Prompter":
        """A Prompter for a plain multi-turn chat call — no Scenario/Env code-gen
        prompt involved (skips ``Scenario.build_prompt``), e.g. for scenario
        ideation, test/exploit generation, or one-off verification calls that
        aren't building an application from an OpenAPI spec.
        """
        self = cls.__new__(cls)
        self.env = None
        self.scenario = None
        self.spec_type = ""
        self.safety_prompt = ""
        self.model = model
        self.provider = provider
        self.batch_size = batch_size
        self.offset = 0
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.vllm_port = 0
        self.use_stubs = False

        self.system_prompt = system_prompt
        self.openai_reasoning = (
            self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
            or self.model.startswith("gpt-5")
        )
        # Whether this call wants thinking is the caller's call, not just a
        # function of the model — see LlmModel in llm/chat.py, which sets this
        # after construction based on its own `reasoning` flag.
        self.anthropic_thinking = self.model in ANTHROPIC_THINKING_LENGTHS

        self.prompt = ""
        self.last_usage = None
        self.conversational = True
        self.history = history
        self.cache_key = None
        return self

    # --- Conversation (delegates to llm.conversation) -----------------

    def _chat_turns(self) -> list[dict[str, str]]:
        return conversation.chat_turns(self)

    def append_user(self, content: str) -> None:
        conversation.append_user(self, content)

    def append_assistant(self, content: str) -> None:
        conversation.append_assistant(self, content)

    def send(self, content: str, logger: logging.Logger) -> str:
        return conversation.send(self, content, logger)

    def send_with_retries(
        self,
        content: str,
        logger: logging.Logger,
        *,
        max_retries: int,
        base_delay: float = 1.0,
        max_delay: float = 128.0,
        log_label: str = "LLM",
    ) -> str:
        return conversation.send_with_retries(
            self,
            content,
            logger,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            log_label=log_label,
        )

    # --- Message / cache helpers (thin wrappers for provider modules) --

    def _system_role(self) -> str | None:
        return messages.system_role(self)

    def _provider_messages(self) -> list[dict[str, str]]:
        return messages.provider_messages(self)

    def _anthropic_system_param(self) -> Any:
        from .cache import anthropic_system_param

        return anthropic_system_param(self)

    def _anthropic_messages(self) -> list[dict[str, Any]]:
        from .cache import anthropic_messages

        return anthropic_messages(self)

    def _openai_cache_kwargs(self) -> dict[str, Any]:
        from .cache import openai_cache_kwargs

        return openai_cache_kwargs(self)

    def _completion_token_budget(self, **kwargs: Any) -> int:
        from .token_budget import completion_token_budget

        return completion_token_budget(self, **kwargs)

    # --- Provider dispatch --------------------------------------------

    def prompt_anthropic(self, logger: logging.Logger) -> list[str]:
        return anthropic_provider.prompt_anthropic(self, logger)

    def prompt_openrouter(self, logger: logging.Logger) -> list[str]:
        return openrouter_provider.prompt_openrouter(self, logger)

    def prompt_vllm(self, logger: logging.Logger) -> list[str]:
        return vllm_provider.prompt_vllm(self, logger)

    def prompt_swissai(self, logger: logging.Logger) -> list[str]:
        return swissai_provider.prompt_swissai(self, logger)

    def prompt_openai_batch(self, logger: logging.Logger) -> list[str]:
        return openai_provider.prompt_openai_batch(self, logger)

    def prompt_togetherai_batch(self, logger: logging.Logger) -> list[str]:
        return together_provider.prompt_togetherai_batch(self, logger)

    def _dump_outgoing_prompt(self, logger: logging.Logger) -> None:
        dump_outgoing_prompt(self, logger)

    def prompt_model(self, logger: logging.Logger) -> list[str]:
        self.last_usage = None
        self._dump_outgoing_prompt(logger)
        if self.provider == "anthropic":
            responses = self.prompt_anthropic(logger)
        elif self.provider == "openrouter":
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
            logger.error("Unknown provider: %s", self.provider)
            raise Exception(f"Unknown provider: {self.provider}")
        if self.last_usage is not None:
            cost_kind = (
                "Reported"
                if self.last_usage.cost_source == "openrouter_reported"
                else "Estimated"
            )
            logger.info(
                "%s LLM cost: $%.4f (%d in + %d out tokens, model=%s)",
                cost_kind,
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
                        logger.info("Retrying %d times", retries)
                    completion = self.prompt_model(logger)
                    if cost_workspace is not None:
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
                        "Got %d/%d responses. Parsing and saving. Raw responses:\n\n%s",
                        len(completion) + i + self.offset,
                        self.batch_size + self.offset,
                        raw_comps,
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
                        logger.error("Max retries reached, raising exception: %s", e)
                        raise
                    delay = min(base_delay * 2**retries, max_delay)
                    delay = random.uniform(0, delay)
                    logger.exception("%s, backing off for %s seconds", e, delay, exc_info=e)
                    time.sleep(delay)

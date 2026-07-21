"""A `Conversation`/`get_model()`-shaped convenience layer over `Prompter`.

`Prompter` is normally built from a `(Env, Scenario)` pair for the main
code-generation flow. This module is for callers that just want to send an
arbitrary multi-turn chat to a model — scenario ideation, test/exploit
generation, ad-hoc verification calls — while still getting the same provider
dispatch and cost tracking as the rest of `llm/`, via `Prompter.for_chat`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .prompter import Prompter
from .usage import TokenUsage

logger = logging.getLogger(__name__)

# Together is spelled "together" by callers but "together_ai" in
# NATIVE_PROVIDER_PREFIXES / the provider dispatch table.
_PROVIDER_MAP = {"together": "together_ai"}


@dataclass
class Response:
    role: str
    text: str
    reasoning: str = ""
    # Set by ChatModel; None for a Response the caller constructed itself
    # (e.g. the user-turn Response added before calling .generate()).
    usage: TokenUsage | None = None

    def __str__(self) -> str:
        return self.text


@dataclass
class Conversation:
    system_prompt: str = (
        "Act as an experienced software developer and provide clear, concise, "
        "and technically accurate responses."
    )
    responses: list[Response] = field(default_factory=list)

    def __str__(self) -> str:
        s = "### System Prompt ###\n"
        s += self.system_prompt + "\n\n"
        for response in self.responses:
            s += f"### {response.role} ###\n"
            s += response.text + "\n\n"
        return s

    def __iter__(self):
        return iter(self.responses)

    def add_message(self, r: Response) -> "Conversation":
        self.responses.append(r)
        return self

    def remove_message(self, index: int = -1) -> Response:
        if not self.responses:
            raise IndexError("No messages to remove")
        return self.responses.pop(index)


class BaseModel(ABC):
    def __init__(
        self,
        model_name: str,
        model_provider: str,
        reasoning: bool = False,
        reasoning_effort: int | str | None = None,
    ):
        self.model_name = model_name
        self.model_provider = model_provider
        self.reasoning = reasoning
        self.reasoning_effort = reasoning_effort

    @abstractmethod
    def _generate_chat(
        self, conversation: Conversation, temperature: float, purpose: str
    ) -> Response: ...

    @abstractmethod
    def _generate_reason(
        self, conversation: Conversation, temperature: float, purpose: str
    ) -> Response: ...

    def generate(
        self, conversation: Conversation, temperature: float, purpose: str = "N/A"
    ) -> Response:
        if self.reasoning:
            return self._generate_reason(conversation, temperature, purpose)
        return self._generate_chat(conversation, temperature, purpose)


class ChatModel(BaseModel):
    """`BaseModel` backed by `Prompter.for_chat` — provider dispatch and cost
    tracking come straight from `llm/providers/*` and `llm/usage.py`.

    Note: for Anthropic models with extended thinking, the provider always
    requests "adaptive" thinking capped by a fixed per-model token budget
    (`ANTHROPIC_THINKING_LENGTHS`), rather than honoring an exact
    `reasoning_effort` token count. The thinking trace is logged but not
    surfaced back into `Response.reasoning`.
    """

    def __init__(
        self,
        model_name: str,
        model_provider: str,
        reasoning: bool = False,
        reasoning_effort: int | str | None = None,
    ):
        super().__init__(model_name, model_provider, reasoning, reasoning_effort)
        self._provider = _PROVIDER_MAP.get(model_provider, model_provider)

    def _build_prompter(self, conversation: Conversation, temperature: float) -> Prompter:
        history = [{"role": r.role, "content": r.text} for r in conversation.responses]
        prompter = Prompter.for_chat(
            model=self.model_name,
            provider=self._provider,
            system_prompt=conversation.system_prompt,
            history=history,
            temperature=temperature,
            reasoning_effort=self.reasoning_effort,
        )
        if self._provider == "anthropic":
            prompter.anthropic_thinking = self.reasoning
        return prompter

    def _run(self, conversation: Conversation, temperature: float, purpose: str) -> Response:
        prompter = self._build_prompter(conversation, temperature)
        completions = prompter.prompt_model(logger)
        if not completions or not completions[0] or not completions[0].strip():
            raise Exception(f"Empty response from {self._provider} API")
        return Response(role="assistant", text=completions[0], usage=prompter.last_usage)

    def _generate_chat(
        self, conversation: Conversation, temperature: float, purpose: str
    ) -> Response:
        return self._run(conversation, temperature, purpose)

    def _generate_reason(
        self, conversation: Conversation, temperature: float, purpose: str
    ) -> Response:
        return self._run(conversation, temperature, purpose)


def get_model(
    model_name: str,
    model_provider: str,
    reasoning: bool = False,
    reasoning_effort: int | str | None = None,
) -> BaseModel:
    if model_provider == "openai":
        if reasoning and isinstance(reasoning_effort, int):
            raise TypeError("OpenAI models do not support token numbers for reasoning.")
    elif model_provider == "together":
        if reasoning and reasoning_effort is not None:
            raise TypeError("Together models do not support reasoning effort settings.")
    elif model_provider == "openrouter":
        if reasoning and reasoning_effort is not None:
            raise TypeError("OpenRouter models do not support reasoning effort settings.")
    elif model_provider == "anthropic":
        if reasoning and isinstance(reasoning_effort, str):
            raise TypeError(
                "Anthropic models require reasoning settings as a number of reasoning tokens."
            )
    else:
        raise NotImplementedError(
            f"Model {model_name} from {model_provider} with reasoning effort {reasoning_effort} is not supported."
        )
    return ChatModel(model_name, model_provider, reasoning, reasoning_effort)

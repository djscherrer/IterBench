"""scenario_builder's per-scenario LLM cost ledger.

Not part of baxbench's llm/ module: the *ledger location* is coupled to
scenario_builder's own ``--path``/``--scenario`` CLI args and artifact
layout, so that part stays local. The actual cost tracking (LlmUsageRecord,
append_usage_record) is baxbench's own shared llm.usage infrastructure,
reused here rather than reimplemented — it gets running totals and a
per-stage cost breakdown (by_call_type) for free.
"""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (baxbench src/ onto sys.path)
from llm import BaseModel, Conversation, Response
from llm import get_model as _llm_get_model
from llm.usage import LlmUsageRecord, TokenUsage, append_usage_record, ledger_path

parser = ArgumentParser()
parser.add_argument("--path", default="./artifacts/")
parser.add_argument("--scenario")

known_args, _ = parser.parse_known_args()
if known_args.scenario:
    LEDGER_WORKSPACE = Path(known_args.path) / known_args.scenario / "logs"
else:
    # No scenario name yet (e.g. mid --generate_scenarios, before the title is
    # known) — write to a workspace-root ledger; scenario_gen/generate.py
    # relocates it into the scenario's own logs/ once the folder exists.
    LEDGER_WORKSPACE = Path(known_args.path)

LEDGER_PATH = ledger_path(LEDGER_WORKSPACE)


def _call_type(purpose: str) -> str:
    """The stage name is everything before the first ": " in the purpose
    string (e.g. "generate_scenario_specs: generating OpenAPI schema" ->
    "generate_scenario_specs"); falls back to the whole string if there's no
    colon (e.g. purpose="N/A" or purpose="test").
    """
    return purpose.split(":", 1)[0].strip()


def record_token_usage(usage: TokenUsage, model: str, provider: str, purpose: str = "N/A") -> None:
    """Append one LLM call's usage to this scenario's cost ledger, bucketed
    by stage (see _call_type)."""
    record = LlmUsageRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        call_type=_call_type(purpose),
        model=model,
        provider=provider,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=usage.estimated_cost_usd,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_source=usage.cost_source,
        note=purpose,
    )
    append_usage_record(LEDGER_WORKSPACE, record)


class _RecordingModel(BaseModel):
    """Wraps a llm.ChatModel so every .generate() call also lands an entry
    in this scenario's cost ledger."""

    def __init__(self, inner: BaseModel):
        super().__init__(
            inner.model_name, inner.model_provider, inner.reasoning, inner.reasoning_effort
        )
        self._inner = inner

    def _record(self, response: Response, purpose: str) -> Response:
        if response.usage is not None:
            record_token_usage(response.usage, self.model_name, self.model_provider, purpose)
        return response

    def _generate_chat(
        self, conversation: Conversation, temperature: float, purpose: str
    ) -> Response:
        return self._record(self._inner._generate_chat(conversation, temperature, purpose), purpose)

    def _generate_reason(
        self, conversation: Conversation, temperature: float, purpose: str
    ) -> Response:
        return self._record(self._inner._generate_reason(conversation, temperature, purpose), purpose)


def get_model(
    model_name: str,
    model_provider: str,
    reasoning: bool = False,
    reasoning_effort: int | str | None = None,
) -> BaseModel:
    return _RecordingModel(_llm_get_model(model_name, model_provider, reasoning, reasoning_effort))

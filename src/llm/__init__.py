"""
LLM prompting, provider adapters, response parsing, and usage tracking.

Public API for BaxBench call sites: ``from llm import Prompter, Parser, …``.
"""

from .keys import KeyLocs
from .parser import Parser
from .prompter import Prompter
from .usage import (
    LlmUsageRecord,
    TokenUsage,
    append_usage_record,
    enforce_cost_budget,
    ensure_cost_section_in_summary,
    estimate_cost_usd,
    format_cost_summary_markdown,
    ledger_path,
    load_ledger,
    load_pricing_table,
    normalize_model_name,
    record_prompter_usage,
    resolve_max_cost_usd,
    usage_from_anthropic,
    usage_from_openai_style,
)

def __getattr__(name: str):
    if name == "OpenHandsPrompter":
        from .openhands import OpenHandsPrompter

        return OpenHandsPrompter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "KeyLocs",
    "LlmUsageRecord",
    "OpenHandsPrompter",
    "Parser",
    "Prompter",
    "TokenUsage",
    "append_usage_record",
    "enforce_cost_budget",
    "ensure_cost_section_in_summary",
    "estimate_cost_usd",
    "format_cost_summary_markdown",
    "ledger_path",
    "load_ledger",
    "load_pricing_table",
    "normalize_model_name",
    "record_prompter_usage",
    "resolve_max_cost_usd",
    "usage_from_anthropic",
    "usage_from_openai_style",
]

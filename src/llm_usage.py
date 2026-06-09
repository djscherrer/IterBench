"""Token usage and estimated LLM cost tracking for BaxBench."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_FILENAME = "llm_cost_ledger.json"

# USD per 1M tokens (input, output). Estimates — override via BAXBENCH_LLM_PRICING_JSON.
_DEFAULT_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-7-sonnet-20250219": (3.0, 15.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1-2025-04-14": (2.0, 8.0),
    "gpt-5-2025-08-07": (5.0, 20.0),
    "gpt-5.4": (5.0, 20.0),
    "o3-mini": (1.1, 4.4),
    "deepseek/deepseek-chat": (0.14, 0.28),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-v3": (0.14, 0.28),
    "deepseek-v3.2": (0.14, 0.28),
    "deepseek/deepseek-v3.2": (0.14, 0.28),
    "meta-llama/llama-3.3-70b-instruct": (0.59, 0.79),
}


@dataclass(frozen=True)
class TokenUsage:
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    # Prompt-cache breakdown (normalized across providers). ``input_tokens`` is
    # the *total* input (cached + uncached); ``cache_read_tokens`` is the cached
    # portion served back at a discount, ``cache_write_tokens`` the portion
    # written into the cache this call. Both default to 0 for providers/runs
    # without caching (DeepSeek, vLLM, …) so older code paths are unaffected.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        """Input tokens billed at full rate (total minus cache reads)."""
        return max(0, self.input_tokens - self.cache_read_tokens)

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of input tokens served from cache (0.0 when no input)."""
        return self.cache_read_tokens / self.input_tokens if self.input_tokens else 0.0


@dataclass
class LlmUsageRecord:
    timestamp: str
    call_type: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int = 0
    iteration_id: str | None = None
    artifact_path: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_model_name(model: str) -> str:
    """Strip provider prefix (``anthropic/claude-opus-4-6`` → ``claude-opus-4-6``)."""
    return model.split("/")[-1].strip().lower()


def load_pricing_table() -> dict[str, tuple[float, float]]:
    raw = os.environ.get("BAXBENCH_LLM_PRICING_JSON", "").strip()
    if not raw:
        return dict(_DEFAULT_PRICING_USD_PER_MTOK)
    try:
        data = json.loads(raw)
        out: dict[str, tuple[float, float]] = {}
        for key, val in data.items():
            if isinstance(val, dict):
                out[normalize_model_name(key)] = (
                    float(val["input"]),
                    float(val["output"]),
                )
            elif isinstance(val, (list, tuple)) and len(val) == 2:
                out[normalize_model_name(key)] = (float(val[0]), float(val[1]))
        return out or dict(_DEFAULT_PRICING_USD_PER_MTOK)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return dict(_DEFAULT_PRICING_USD_PER_MTOK)


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> float:
    table = pricing or load_pricing_table()
    key = normalize_model_name(model)
    rates = table.get(key)
    if rates is None:
        for pat, rate in table.items():
            if key.startswith(pat) or pat in key:
                rates = rate
                break
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


def _openai_style_cache_tokens(usage: Any) -> tuple[int, int]:
    """
    Cache (read, write) tokens from an OpenAI-style ``usage`` object.

    OpenAI exposes cache reads via ``prompt_tokens_details.cached_tokens`` (no
    write field — it has no write premium). OpenRouter additionally surfaces
    ``cache_write_tokens`` on the same nested object. Both are absent on
    providers without caching, hence the defensive ``getattr``.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0, 0
    read = int(getattr(details, "cached_tokens", 0) or 0)
    write = int(getattr(details, "cache_write_tokens", 0) or 0)
    return read, write


def usage_from_openai_style(
    usage: Any,
    *,
    model: str,
    provider: str,
) -> TokenUsage | None:
    if usage is None:
        return None
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    if prompt == 0 and completion == 0:
        return None
    cache_read, cache_write = _openai_style_cache_tokens(usage)
    # OpenAI-style ``prompt_tokens`` already counts cached + uncached, so it is
    # our normalized total input directly.
    cost = estimate_cost_usd(model, prompt, completion)
    return TokenUsage(
        model=model,
        provider=provider,
        input_tokens=prompt,
        output_tokens=completion,
        estimated_cost_usd=cost,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def usage_from_anthropic(
    usage: Any,
    *,
    model: str,
) -> TokenUsage | None:
    if usage is None:
        return None
    # Anthropic's ``input_tokens`` is only the *uncached* tail; cache reads and
    # writes are reported separately. Normalize to a single total input so the
    # field means the same thing as the OpenAI-style path.
    uncached = int(getattr(usage, "input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    completion = int(getattr(usage, "output_tokens", 0) or 0)
    total_input = uncached + cache_read + cache_write
    if total_input == 0 and completion == 0:
        return None
    cost = estimate_cost_usd(model, total_input, completion)
    return TokenUsage(
        model=model,
        provider="anthropic",
        input_tokens=total_input,
        output_tokens=completion,
        estimated_cost_usd=cost,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def resolve_max_cost_usd() -> float | None:
    raw = os.environ.get("BAXBENCH_LLM_MAX_COST", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def ledger_path(workspace: Path) -> Path:
    return workspace / LEDGER_FILENAME


def load_ledger(workspace: Path) -> dict[str, Any]:
    path = ledger_path(workspace)
    if not path.is_file():
        return _empty_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else _empty_ledger()
    except (OSError, json.JSONDecodeError):
        return _empty_ledger()


def _empty_ledger() -> dict[str, Any]:
    return {
        "currency": "USD",
        "pricing_note": (
            "Estimated from built-in per-model $/MTok table. "
            "Override with BAXBENCH_LLM_PRICING_JSON. "
            "Set BAXBENCH_LLM_MAX_COST to cap spend."
        ),
        "max_cost_usd": resolve_max_cost_usd(),
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "calls": [],
        "by_call_type": {},
        "by_iteration": {},
    }


def enforce_cost_budget(workspace: Path) -> None:
    """Raise if experiment ledger exceeds ``BAXBENCH_LLM_MAX_COST``."""
    max_cost = resolve_max_cost_usd()
    if max_cost is None:
        return
    ledger = load_ledger(workspace)
    total = float(ledger.get("total_cost_usd", 0.0))
    if total >= max_cost:
        raise RuntimeError(
            f"LLM cost budget exceeded: ${total:.4f} >= ${max_cost:.4f} "
            f"(ledger: {ledger_path(workspace)}). "
            "Raise BAXBENCH_LLM_MAX_COST or reset the ledger to continue."
        )


def append_usage_record(
    workspace: Path,
    record: LlmUsageRecord,
) -> dict[str, Any]:
    """Append one call to the workspace ledger and return updated totals."""
    enforce_cost_budget(workspace)

    ledger = load_ledger(workspace)
    ledger["max_cost_usd"] = resolve_max_cost_usd()
    ledger["total_cost_usd"] = round(
        float(ledger.get("total_cost_usd", 0.0)) + record.estimated_cost_usd, 6
    )
    ledger["total_input_tokens"] = int(ledger.get("total_input_tokens", 0)) + record.input_tokens
    ledger["total_output_tokens"] = int(ledger.get("total_output_tokens", 0)) + record.output_tokens
    ledger["total_cache_read_tokens"] = (
        int(ledger.get("total_cache_read_tokens", 0)) + record.cache_read_tokens
    )
    ledger["total_cache_write_tokens"] = (
        int(ledger.get("total_cache_write_tokens", 0)) + record.cache_write_tokens
    )

    calls = ledger.setdefault("calls", [])
    calls.append(record.to_dict())

    by_type: dict[str, float] = ledger.setdefault("by_call_type", {})
    by_type[record.call_type] = round(
        float(by_type.get(record.call_type, 0.0)) + record.estimated_cost_usd, 6
    )

    if record.iteration_id:
        by_iter: dict[str, float] = ledger.setdefault("by_iteration", {})
        by_iter[record.iteration_id] = round(
            float(by_iter.get(record.iteration_id, 0.0)) + record.estimated_cost_usd, 6
        )

    path = ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    enforce_cost_budget(workspace)
    return ledger


def record_prompter_usage(
    *,
    prompter: Any,
    call_type: str,
    workspace: Path,
    logger: Any,
    artifact_dir: Path | None = None,
    iteration_id: str | None = None,
    note: str | None = None,
) -> LlmUsageRecord | None:
    """Persist usage from ``prompter.last_usage`` to ledger + optional local artifact."""
    usage: TokenUsage | None = getattr(prompter, "last_usage", None)
    if usage is None:
        logger.warning("LLM call %s: token usage unavailable (cost not tracked)", call_type)
        return None

    record = LlmUsageRecord(
        timestamp=_utc_now(),
        call_type=call_type,
        model=usage.model,
        provider=usage.provider,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=round(usage.estimated_cost_usd, 6),
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        uncached_input_tokens=usage.uncached_input_tokens,
        iteration_id=iteration_id,
        note=note,
    )

    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        local_path = artifact_dir / "llm_usage.json"
        payload = record.to_dict()
        payload["pricing_usd_per_mtok"] = load_pricing_table().get(
            normalize_model_name(usage.model)
        )
        local_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record.artifact_path = str(local_path)

    ledger = append_usage_record(workspace, record)
    logger.info(
        "LLM %s: in=%d (cache: read=%d write=%d uncached=%d, hit=%.0f%%) "
        "out=%d ~$%.4f (experiment total ~$%.4f)",
        call_type,
        usage.input_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.uncached_input_tokens,
        usage.cache_hit_ratio * 100.0,
        usage.output_tokens,
        usage.estimated_cost_usd,
        float(ledger.get("total_cost_usd", 0.0)),
    )
    return record


def format_cost_summary_markdown(workspace: Path) -> str:
    ledger = load_ledger(workspace)
    total = float(ledger.get("total_cost_usd", 0.0))
    if total <= 0 and not ledger.get("calls"):
        return ""
    lines = [
        "## LLM cost (estimated)",
        "",
        f"- **Total**: ~${total:.4f} USD",
        f"- **Tokens**: {ledger.get('total_input_tokens', 0):,} in / "
        f"{ledger.get('total_output_tokens', 0):,} out",
    ]
    cache_read = int(ledger.get("total_cache_read_tokens", 0))
    cache_write = int(ledger.get("total_cache_write_tokens", 0))
    if cache_read or cache_write:
        total_in = int(ledger.get("total_input_tokens", 0))
        hit = (cache_read / total_in * 100.0) if total_in else 0.0
        lines.append(
            f"- **Prompt cache**: {cache_read:,} read / {cache_write:,} write "
            f"({hit:.0f}% of input served from cache)"
        )
    max_cost = ledger.get("max_cost_usd")
    if max_cost is not None:
        lines.append(f"- **Budget cap**: ${float(max_cost):.4f} USD")
    by_type = ledger.get("by_call_type") or {}
    if by_type:
        lines.append("- **By call type**:")
        for kind, cost in sorted(by_type.items()):
            lines.append(f"  - `{kind}`: ~${float(cost):.4f}")
    lines.append("")
    return "\n".join(lines)


def ensure_cost_section_in_summary(summary_path: Path, workspace: Path) -> None:
    """Insert or refresh the cost block at the top of experiment_summary.md."""
    block = format_cost_summary_markdown(workspace)
    if not block or not summary_path.is_file():
        return
    text = summary_path.read_text(encoding="utf-8")
    marker_start = "<!-- baxbench-llm-cost -->"
    marker_end = "<!-- /baxbench-llm-cost -->"
    wrapped = f"{marker_start}\n{block}{marker_end}\n\n"
    pattern = re.compile(
        re.escape(marker_start) + r".*?" + re.escape(marker_end) + r"\n*",
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(wrapped, text)
    else:
        # After the header intro (first ---)
        parts = text.split("\n---\n", 1)
        if len(parts) == 2:
            text = parts[0] + "\n---\n\n" + wrapped + parts[1]
        else:
            text = wrapped + text
    summary_path.write_text(text, encoding="utf-8")

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


# OpenAI's gpt-5.x flagship models charge a higher "long context" rate for the
# ENTIRE request once its total input exceeds this many tokens. Conversational
# k8s runs cross it around iteration ~5 (see llm_cost_ledger.json), so cost has
# to switch tiers per call rather than assuming one or the other.
OPENAI_LONG_CONTEXT_THRESHOLD = 128_000


@dataclass(frozen=True)
class ModelPricing:
    """Per-model price list in USD per 1M tokens.

    The four base rates are independent buckets so prompt caching is priced
    correctly instead of charging every input token at the full ``input`` rate:

    - ``input``: uncached prompt tokens, billed at the standard input rate. This
      is the only input rate that applies when caching is off.
    - ``output``: generated completion tokens.
    - ``cache_read``: cached prompt tokens served back on a cache *hit*. Both
      OpenAI and Anthropic discount these heavily (OpenAI ~0.1–0.5× input
      depending on the model family, Anthropic a flat 0.1× input). Leave
      ``None`` when no discount is known/applicable — reads then fall back to
      the full ``input`` rate via :meth:`read_rate`.
    - ``cache_write``: prompt tokens written into the cache on a *miss*. OpenAI
      charges no write premium, so leave it ``None`` (writes bill as plain
      input). Anthropic charges a premium that depends on the cache TTL:
      1.25× input for the 5m TTL, 2× input for the 1h TTL. Leave ``None`` to
      fall back to the full ``input`` rate via :meth:`write_rate`.

    ``long``/``long_context_threshold`` model OpenAI's two-tier context pricing:
    when a request's total input exceeds the threshold, the *whole* request is
    billed at the ``long`` rates. Models without a long tier leave both unset.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    long: "ModelPricing | None" = None
    long_context_threshold: int = 0

    def read_rate(self) -> float:
        """Rate for cached-input reads ($/MTok), defaulting to full input."""
        return self.cache_read if self.cache_read is not None else self.input

    def write_rate(self) -> float:
        """Rate for cache writes ($/MTok), defaulting to full input."""
        return self.cache_write if self.cache_write is not None else self.input

    def tier_for(self, total_input_tokens: int) -> "ModelPricing":
        """Pick the short- or long-context tier for a request of this size."""
        if (
            self.long is not None
            and self.long_context_threshold
            and total_input_tokens > self.long_context_threshold
        ):
            return self.long
        return self


# Per-model USD/MTok price list. Estimates — keep in sync with provider docs.
#
# Cache rates follow each provider's published multipliers relative to ``input``:
#   * OpenAI: cached reads are discounted (~0.5× for 4o/o-series, ~0.25× for
#     gpt-4.1, 0.1× for gpt-5.x); no write premium → ``cache_write`` left None.
#     gpt-5.x flagships additionally have a long-context tier (see ``long``).
#   * Anthropic: cached reads are 0.1× input; cache *writes* are 2× input for
#     the 1h TTL (see ANTHROPIC_CACHE_TTL in llm/cache.py). IMPORTANT: if that
#     TTL is changed to "5m", the Anthropic ``cache_write`` rates below must be
#     recomputed as 1.25× input. The two locations are coupled.
#   * DeepSeek via OpenRouter: caching is automatic (implicit prefix cache +
#     provider sticky routing); cache reads are ~0.1× input and there is no
#     write premium, so ``cache_read`` is set and ``cache_write`` left None.
#   * Other open models (Llama via OpenRouter/vLLM): no prompt caching wired,
#     so cache rates are left None and never charged.
#
# More-specific keys (e.g. gpt-5.4-mini) are listed before their prefixes
# (gpt-5.4) because lookup_pricing() falls back to substring matching in order.
MODEL_PRICING: dict[str, ModelPricing] = {
    # --- Anthropic Claude (1h cache TTL) ---
    "claude-opus-4-8": ModelPricing(5.0, 25.0, cache_read=0.5, cache_write=10.0),
    "claude-opus-4-7": ModelPricing(5.0, 25.0, cache_read=0.5, cache_write=10.0),
    "claude-opus-4-6": ModelPricing(5.0, 25.0, cache_read=0.5, cache_write=10.0),
    "claude-opus-4-5": ModelPricing(5.0, 25.0, cache_read=0.5, cache_write=10.0),
    "claude-opus-4-20250514": ModelPricing(15.0, 75.0, cache_read=1.5, cache_write=30.0),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, cache_read=0.3, cache_write=6.0),
    "claude-sonnet-4-5": ModelPricing(3.0, 15.0, cache_read=0.3, cache_write=6.0),
    "claude-sonnet-4-20250514": ModelPricing(3.0, 15.0, cache_read=0.3, cache_write=6.0),
    "claude-3-7-sonnet-20250219": ModelPricing(3.0, 15.0, cache_read=0.3, cache_write=6.0),
    "claude-haiku-4-5": ModelPricing(1.0, 5.0, cache_read=0.1, cache_write=2.0),
    # --- OpenAI gpt-5.x (long-context tier applies >128K total input) ---
    "gpt-5.5-pro": ModelPricing(
        30.0, 180.0, long=ModelPricing(60.0, 270.0),
        long_context_threshold=OPENAI_LONG_CONTEXT_THRESHOLD,
    ),
    "gpt-5.5": ModelPricing(
        5.0, 30.0, cache_read=0.5,
        long=ModelPricing(10.0, 45.0, cache_read=1.0),
        long_context_threshold=OPENAI_LONG_CONTEXT_THRESHOLD,
    ),
    "gpt-5.4-pro": ModelPricing(
        30.0, 180.0, long=ModelPricing(60.0, 270.0),
        long_context_threshold=OPENAI_LONG_CONTEXT_THRESHOLD,
    ),
    "gpt-5.4-mini": ModelPricing(0.75, 4.5, cache_read=0.075),
    "gpt-5.4-nano": ModelPricing(0.2, 1.25, cache_read=0.02),
    "gpt-5.4": ModelPricing(
        2.5, 15.0, cache_read=0.25,
        long=ModelPricing(5.0, 22.5, cache_read=0.5),
        long_context_threshold=OPENAI_LONG_CONTEXT_THRESHOLD,
    ),
    # --- OpenAI legacy ---
    "gpt-5-2025-08-07": ModelPricing(5.0, 20.0, cache_read=0.5),
    "gpt-4o": ModelPricing(2.5, 10.0, cache_read=1.25),
    "gpt-4.1-2025-04-14": ModelPricing(2.0, 8.0, cache_read=0.5),
    "o3-mini": ModelPricing(1.1, 4.4, cache_read=0.55),
    # --- DeepSeek via OpenRouter (automatic cache, ~0.1× input on reads) ---
    "deepseek/deepseek-v4-pro": ModelPricing(0.435, 0.87),
    "deepseek-v4-pro": ModelPricing(0.435, 0.87),
    "deepseek/deepseek-chat": ModelPricing(0.14, 0.28, cache_read=0.014),
    "deepseek-chat": ModelPricing(0.14, 0.28, cache_read=0.014),
    "deepseek-v3": ModelPricing(0.14, 0.28, cache_read=0.014),
    "deepseek-v3.2": ModelPricing(0.14, 0.28, cache_read=0.014),
    "deepseek/deepseek-v3.2": ModelPricing(0.14, 0.28, cache_read=0.014),
    # --- Other open models (no prompt-cache pricing wired) ---
    "meta-llama/llama-3.3-70b-instruct": ModelPricing(0.59, 0.79),
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
    # ``openrouter_reported`` when ``usage.cost`` came back from OpenRouter;
    # otherwise ``estimated`` from :data:`MODEL_PRICING`.
    cost_source: str = "estimated"

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
    cost_source: str = "estimated"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_model_name(model: str) -> str:
    """Strip provider prefix (``anthropic/claude-opus-4-6`` → ``claude-opus-4-6``)."""
    return model.split("/")[-1].strip().lower()


def lookup_pricing(model: str) -> ModelPricing | None:
    """Resolve the :class:`ModelPricing` for ``model`` (exact, then substring)."""
    key = normalize_model_name(model)
    pricing = MODEL_PRICING.get(key)
    if pricing is not None:
        return pricing
    for pat, candidate in MODEL_PRICING.items():
        if key.startswith(pat) or pat in key:
            return candidate
    return None


def estimate_cost_usd(
    model: str,
    *,
    uncached_input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    pricing: ModelPricing | None = None,
) -> float:
    """Cache-aware cost estimate: each token bucket is billed at its own rate.

    For models with a long-context tier (OpenAI gpt-5.x), the whole request is
    priced at the long rates once total input crosses the threshold.
    """
    rates = pricing or lookup_pricing(model)
    if rates is None:
        return 0.0
    total_input = uncached_input_tokens + cache_read_tokens + cache_write_tokens
    rates = rates.tier_for(total_input)
    return (
        uncached_input_tokens * rates.input
        + cache_read_tokens * rates.read_rate()
        + cache_write_tokens * rates.write_rate()
        + output_tokens * rates.output
    ) / 1_000_000.0


def _reported_cost_usd(usage: Any) -> float | None:
    """OpenRouter's authoritative per-request charge (USD), when present."""
    cost = getattr(usage, "cost", None)
    if cost is None and isinstance(usage, dict):
        cost = usage.get("cost")
    if cost is None:
        return None
    try:
        val = float(cost)
    except (TypeError, ValueError):
        return None
    return val if val >= 0 else None


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
    # OpenAI-style ``prompt_tokens`` already counts cached + uncached, so the
    # uncached tail billed at full rate is the total minus the cached reads.
    reported = _reported_cost_usd(usage) if provider == "openrouter" else None
    if reported is not None:
        cost = reported
        cost_source = "openrouter_reported"
    else:
        cost = estimate_cost_usd(
            model,
            uncached_input_tokens=max(0, prompt - cache_read),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=completion,
        )
        cost_source = "estimated"
    return TokenUsage(
        model=model,
        provider=provider,
        input_tokens=prompt,
        output_tokens=completion,
        estimated_cost_usd=cost,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_source=cost_source,
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
    # Anthropic already reports the uncached tail separately, so each bucket
    # maps straight onto its own rate (reads 0.1×, writes 2× input for 1h TTL).
    cost = estimate_cost_usd(
        model,
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=completion,
    )
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
            "OpenRouter: uses usage.cost from the API when present; otherwise "
            "estimated from the built-in per-model $/MTok table in "
            "llm/usage.py (cache reads/writes priced separately). "
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
        cost_source=usage.cost_source,
    )

    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        local_path = artifact_dir / "llm_usage.json"
        payload = record.to_dict()
        pricing = lookup_pricing(usage.model)
        payload["pricing_usd_per_mtok"] = asdict(pricing) if pricing else None
        if usage.cost_source == "openrouter_reported":
            payload["pricing_usd_per_mtok"] = None
        local_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record.artifact_path = str(local_path)

    ledger = append_usage_record(workspace, record)
    cost_label = (
        "reported" if usage.cost_source == "openrouter_reported" else "estimated"
    )
    logger.info(
        "LLM %s: in=%d (cache: read=%d write=%d uncached=%d, hit=%.0f%%) "
        "out=%d %s cost ~$%.4f (experiment total ~$%.4f)",
        call_type,
        usage.input_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.uncached_input_tokens,
        usage.cache_hit_ratio * 100.0,
        usage.output_tokens,
        cost_label,
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
        "## LLM cost",
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

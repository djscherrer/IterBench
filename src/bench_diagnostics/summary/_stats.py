"""Small numeric helpers for diagnostics summaries."""

from __future__ import annotations

DISTRIBUTION_LEGEND = (
    "Numeric values are **min / p50 / avg / p95 / max** over samples in the run."
)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    weight = rank - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight


def median(values: list[float] | list[int]) -> float:
    if not values:
        return 0.0
    s = sorted(float(v) for v in values)
    return _percentile(s, 0.5)


def p95(values: list[float] | list[int]) -> float:
    if not values:
        return 0.0
    s = sorted(float(v) for v in values)
    return _percentile(s, 0.95)


def min_avg_max_int(values: list[int]) -> str:
    if not values:
        return "-"
    return f"{min(values)}/{int(round(sum(values) / len(values)))}/{max(values)}"


def min_avg_max_float(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.1f}/{sum(values) / len(values):.1f}/{max(values):.1f}"


def distribution_int(values: list[int]) -> str:
    """``min/p50/avg/p95/max`` for integer samples."""
    if not values:
        return "-"
    avg = int(round(sum(values) / len(values)))
    med = int(round(median(values)))
    p95v = int(round(p95(values)))
    return f"{min(values)}/{med}/{avg}/{p95v}/{max(values)}"


def distribution_float(values: list[float], *, precision: int = 1) -> str:
    """``min/p50/avg/p95/max`` for float samples."""
    if not values:
        return "-"
    fmt = f"{{:.{precision}f}}"
    avg = sum(values) / len(values)
    return "/".join(
        fmt.format(v)
        for v in (min(values), median(values), avg, p95(values), max(values))
    )

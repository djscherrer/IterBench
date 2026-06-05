"""Small numeric helpers for diagnostics summaries."""

from __future__ import annotations


def min_avg_max_int(values: list[int]) -> str:
    if not values:
        return "-"
    return f"{min(values)}/{int(round(sum(values) / len(values)))}/{max(values)}"


def min_avg_max_float(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.1f}/{sum(values) / len(values):.1f}/{max(values):.1f}"

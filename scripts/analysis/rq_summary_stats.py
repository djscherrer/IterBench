#!/usr/bin/env python3
"""
Authoritative RQ1 / RQ3 / RQ4 summary statistics for the thesis Evaluation
chapter, using geometric means throughout for goodput and gain values.

This is the single source for every number quoted in Table 6.1 ("Baseline
vs. best-iteration goodput") and the RQ4 framework-comparison prose; it
reads ``results_aggregate/cells.csv`` (produced by
``aggregate_evaluation.py``) so it never needs to touch raw ``results/``
data or hand-compute anything.

Geometric-mean rule: a cell that never records a positive goodput value
(``max_goodput_rps == 0``) has no valid gain reference and is excluded
from every geometric-mean aggregate here, explicitly and by name, not
silently dropped or zero-filled. "Healthy baseline" (baseline_goodput_rps
> 0, i.e. iteration 000 itself was already serving traffic) is a stricter,
separate condition kept for its own sake, not used to gate the gain
calculation, which instead uses the first iteration with any positive
goodput regardless of index.

Usage:
    pipenv run python scripts/analysis/rq_summary_stats.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from plots.aggregate.tables import geometric_mean  # noqa: E402

_MODEL_ORDER = [
    "anthropic-claude-opus-4-8",
    "openai-gpt-5.5-2026-04-23",
    "z-ai-glm-5.2",
]


def rq1_table(cells: pd.DataFrame) -> pd.DataFrame:
    """Per-model RQ1 summary: first-non-zero, best, healthy baseline, gain."""
    rows = []
    for model in _MODEL_ORDER:
        sub = cells[cells["model"] == model]
        n_total = len(sub)
        n_healthy_baseline = int((sub["baseline_goodput_rps"] > 0).sum())

        valid = sub.dropna(subset=["first_nonzero_goodput_rps", "max_goodput_rps"])
        n_excluded = n_total - len(valid)
        excluded_cells = sub[sub["first_nonzero_goodput_rps"].isna()]

        gm_first_nonzero = geometric_mean(list(valid["first_nonzero_goodput_rps"]))
        gm_best = geometric_mean(list(valid["max_goodput_rps"]))
        gm_gain = geometric_mean(list(valid["gain_first_nonzero"]))
        gain_min = valid["gain_first_nonzero"].min()
        gain_max = valid["gain_first_nonzero"].max()

        rows.append(
            {
                "model": model,
                "n_total": n_total,
                "n_valid": len(valid),
                "n_excluded": n_excluded,
                "excluded_cells": ", ".join(
                    f"{r.scenario}x{r.env}" for r in excluded_cells.itertuples()
                ),
                "n_healthy_baseline": n_healthy_baseline,
                "gm_first_nonzero_goodput": gm_first_nonzero,
                "gm_best_goodput": gm_best,
                "gm_gain": gm_gain,
                "gain_min": gain_min,
                "gain_max": gain_max,
            }
        )
    return pd.DataFrame(rows)


def rq4_table(cells: pd.DataFrame) -> pd.DataFrame:
    """Per-model x per-framework RQ4 summary: geometric mean of best goodput."""
    positive = cells[cells["max_goodput_rps"] > 0]
    rows = []
    for model in _MODEL_ORDER:
        for env in sorted(cells["env"].unique()):
            sub = positive[(positive["model"] == model) & (positive["env"] == env)]
            n_total_env = len(cells[(cells["model"] == model) & (cells["env"] == env)])
            rows.append(
                {
                    "model": model,
                    "env": env,
                    "n_valid": len(sub),
                    "n_total": n_total_env,
                    "gm_best_goodput": geometric_mean(list(sub["max_goodput_rps"])),
                }
            )
    return pd.DataFrame(rows)


def main() -> int:
    cells_path = _REPO_ROOT / "results_aggregate" / "cells.csv"
    if not cells_path.is_file():
        print(f"missing {cells_path}; run aggregate_evaluation.py first", file=sys.stderr)
        return 2
    cells = pd.read_csv(cells_path)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print("== RQ1: first-non-zero vs. best goodput, geometric means ==")
    print(rq1_table(cells).round(2).to_string(index=False))

    print("\n== RQ4: best goodput per model x framework, geometric mean ==")
    print(rq4_table(cells).round(2).to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

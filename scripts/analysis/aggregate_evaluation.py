#!/usr/bin/env python3
"""
Cross-experiment evaluation aggregation for the thesis results chapter.

Walks ``results/<model>/<scenario>/<env>/<variant>/sampleN/k8s-experiments/<slug>/``
for every cell that has an ``iterations/`` tree, and produces:

- ``cells.csv``      one row per scenario x framework x model cell (baseline /
                      final / best goodput, completion status, LLM cost).
- ``iterations.csv``  one row per successful iteration (goodput, refinement
                      kind, delta vs. previous iteration, spec knobs).
- ``failures.csv``   one row per recorded ``failure.json`` (phase, kind).
- ``figures/*.png`` + ``*.pdf``  the plots in ``plots.aggregate.figures``.

Usage:
    pipenv run python scripts/analysis/aggregate_evaluation.py \\
        --exclude-models deepseek-deepseek-v3.2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from plots.aggregate.tables import collect_all  # noqa: E402
from plots.aggregate.figures import generate_all_figures  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results-root", type=Path, default=_REPO_ROOT / "results")
    ap.add_argument("--experiment-slug", default="results")
    ap.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "results_aggregate")
    ap.add_argument("--include-models", nargs="*", default=None)
    ap.add_argument("--exclude-models", nargs="*", default=None)
    ap.add_argument("--no-figures", action="store_true", help="Only write CSVs.")
    args = ap.parse_args()

    if not args.results_root.is_dir():
        print(f"results root not found: {args.results_root}", file=sys.stderr)
        return 2

    data = collect_all(
        args.results_root,
        experiment_slug=args.experiment_slug,
        include_models=set(args.include_models) if args.include_models else None,
        exclude_models=set(args.exclude_models) if args.exclude_models else None,
    )

    if data.cells.empty:
        print("No matching k8s experiment cells found.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    data.cells.to_csv(args.out_dir / "cells.csv", index=False)
    data.iterations.to_csv(args.out_dir / "iterations.csv", index=False)
    data.failures.to_csv(args.out_dir / "failures.csv", index=False)
    print(f"Wrote {args.out_dir / 'cells.csv'} ({len(data.cells)} rows)")
    print(f"Wrote {args.out_dir / 'iterations.csv'} ({len(data.iterations)} rows)")
    print(f"Wrote {args.out_dir / 'failures.csv'} ({len(data.failures)} rows)")

    print("\n== Per-model summary ==")
    summary = (
        data.cells.groupby("model")
        .agg(
            n_cells=("scenario", "count"),
            n_reached_baseline=("reached_baseline", "sum"),
            median_baseline_goodput=("baseline_goodput_rps", "median"),
            median_best_goodput=("max_goodput_rps", "median"),
            total_llm_cost_usd=("total_llm_cost_usd", "sum"),
        )
        .round(2)
    )
    print(summary.to_string())

    if not args.no_figures:
        figures_dir = args.out_dir / "figures"
        created = generate_all_figures(data, figures_dir)
        print(f"\nWrote {len(created)} figure file(s) to {figures_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

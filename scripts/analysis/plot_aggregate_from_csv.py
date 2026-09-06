#!/usr/bin/env python3
"""Regenerate aggregate evaluation figures from the tracked CSV release.

Unlike ``aggregate_evaluation.py``, this command does not need the full raw
``results/`` tree. It is intended for readers who want to recreate the
cross-task figures from the compact public aggregate dataset.

Usage:
    pipenv run python scripts/analysis/plot_aggregate_from_csv.py \
        --aggregate-dir results_aggregate --out-dir /tmp/iterbench-figures
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from plots.aggregate.figures import generate_all_figures  # noqa: E402
from plots.aggregate.tables import AggregateData  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aggregate-dir",
        type=Path,
        default=_REPO_ROOT / "results_aggregate",
        help="Directory containing cells.csv, iterations.csv, and failures.csv.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_REPO_ROOT / "results_aggregate" / "figures",
        help="Directory to receive PNG and PDF figure pairs.",
    )
    args = parser.parse_args()

    required = ("cells.csv", "iterations.csv", "failures.csv")
    missing = [name for name in required if not (args.aggregate_dir / name).is_file()]
    if missing:
        parser.error(
            f"missing {', '.join(missing)} in {args.aggregate_dir}; "
            "pass --aggregate-dir with the published aggregate files"
        )

    data = AggregateData(
        cells=pd.read_csv(args.aggregate_dir / "cells.csv"),
        iterations=pd.read_csv(args.aggregate_dir / "iterations.csv"),
        failures=pd.read_csv(args.aggregate_dir / "failures.csv"),
    )
    created = generate_all_figures(data, args.out_dir)
    print(f"wrote {len(created)} figure file(s) to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

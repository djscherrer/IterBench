#!/usr/bin/env python3
"""
Trajectory small-multiples, annotated variant.

Same scenario x framework grid as ``plot_goodput_trajectories_grid``, but it
distinguishes four measurement states:

* filled circle  -- sustained-goodput estimate established by refine
* hollow circle  -- transient explore-phase peak for iterations with no
                    sustained estimate but demonstrable service
* filled square  -- no positive service observed during warm-up
* cross at y=0   -- the iteration failed before reaching bench at all
                    (``failure.json``; no goodput of either kind exists)

Usage::

    .venv/bin/python scripts/analysis/plot_trajectories_with_explore.py \
        --phases results_aggregate/load_profile_phases.csv \
        --out-dir Writeup/figures/eval
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

from plots.aggregate.figures import (  # noqa: E402
    _INK_PRIMARY,
    _INK_SECONDARY,
    _model_colors,
    _save,
    _short_model,
    _short_scenario,
    _style_axes,
)


def _truthy(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1"])


def build(phases: Path, iterations: Path, failures: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    ph = pd.read_csv(phases)
    it = pd.read_csv(iterations)
    fa = pd.read_csv(failures)

    key = ["model", "scenario", "env", "sample", "iteration_index"]
    ph["sustained"] = pd.to_numeric(ph["sustained_goodput_rps"], errors="coerce").fillna(0.0)
    ph["peak"] = pd.to_numeric(ph["explore_peak_goodput_rps"], errors="coerce")
    ph["warm_ok"] = _truthy(ph["warmup_healthy"])

    pts = ph[key + ["sustained", "peak", "warm_ok"]].copy()
    # keep the same row population the main grid plots
    pts = pts.merge(it[key].drop_duplicates(), on=key, how="inner")

    fa = fa.dropna(subset=["iteration_index"]).copy()
    fa["iteration_index"] = fa["iteration_index"].astype(int)
    if "sample" not in fa.columns:
        fa["sample"] = 0
    return pts, fa


def plot(pts: pd.DataFrame, fails: pd.DataFrame, out_dir: Path, stem: str) -> list[Path]:
    scenarios = sorted(pts["scenario"].unique())
    envs = sorted(pts["env"].unique())
    models = sorted(pts["model"].unique())
    colors = _model_colors(models)

    fig, axes = plt.subplots(
        len(scenarios), len(envs),
        figsize=(3.4 * len(envs), 2.10 * len(scenarios)),
        squeeze=False, sharex=False,
    )

    for r, scenario in enumerate(scenarios):
        for c, env in enumerate(envs):
            ax = axes[r][c]
            cell = pts[(pts["scenario"] == scenario) & (pts["env"] == env)]
            fcell = fails[(fails["scenario"] == scenario) & (fails["env"] == env)]
            if cell.empty:
                ax.axis("off")
                continue

            for model in models:
                sub = cell[cell["model"] == model].sort_values("iteration_index")
                if sub.empty:
                    continue
                col = colors[model]
                # Positive sustained estimate, transient service without an
                # estimate, and no positive service are distinct states.
                trans_mask = (sub["sustained"] <= 0) & (sub["peak"] > 0)
                no_service_mask = (sub["sustained"] <= 0) & ~trans_mask

                # The series uses the sustained estimate where one exists, the
                # transient peak where traffic was served, and zero only as the
                # visual location for "no positive service observed". Segments
                # touching either no-estimate state are dashed.
                xs = sub["iteration_index"].to_numpy()
                ys = sub["sustained"].where(~trans_mask, sub["peak"]).to_numpy()
                no_estimate = (trans_mask | no_service_mask).to_numpy()
                for i in range(len(xs) - 1):
                    kw = {"dashes": (3, 2)} if (no_estimate[i] or no_estimate[i + 1]) else {}
                    ax.plot(xs[i:i + 2], ys[i:i + 2], color=col, linewidth=2.0,
                            zorder=2, **kw)

                served = sub[sub["sustained"] > 0]
                ax.plot(served["iteration_index"], served["sustained"], linestyle="none",
                        marker="o", markersize=4.5, color=col, zorder=3)

                # Hollow marker: served in explore, but refine established no
                # sustained estimate.
                trans = sub[trans_mask]
                ax.plot(trans["iteration_index"], trans["peak"], linestyle="none",
                        marker="o", markersize=5.5, markerfacecolor="none",
                        markeredgecolor=col, markeredgewidth=1.4, zorder=4)

                no_service = sub[no_service_mask]
                ax.plot(no_service["iteration_index"], [0] * len(no_service),
                        linestyle="none", marker="s", markersize=5.2,
                        markerfacecolor=col, markeredgecolor=col, zorder=4,
                        clip_on=False)

                fsub = fcell[fcell["model"] == model]
                if not fsub.empty:
                    ax.plot(fsub["iteration_index"], [0] * len(fsub), linestyle="none",
                            marker="x", markersize=7.5, markeredgewidth=2.2,
                            color=col, zorder=5, clip_on=False)

            _style_axes(ax)
            top = ax.get_ylim()[1]
            ax.set_ylim(0, top if top > 1 else 1)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=8, min_n_ticks=6))
            if r == 0:
                ax.set_title(env, fontsize=14, fontweight="bold", color=_INK_PRIMARY)
            if c == 0:
                ax.set_ylabel(_short_scenario(scenario), fontsize=13,
                              fontweight="bold", color=_INK_PRIMARY)
            ax.tick_params(labelsize=11)

    handles = [Line2D([], [], color=colors[m], marker="o", markersize=6, linewidth=2.0)
               for m in models]
    labels = [_short_model(m) for m in models]
    handles += [
        Line2D([], [], color=_INK_SECONDARY, marker="o", markersize=6,
               linestyle="none", label="sustained estimate"),
        Line2D([], [], color=_INK_SECONDARY, marker="o", markersize=7, linestyle="none",
               markerfacecolor="none", markeredgewidth=1.4, label="transient explore peak"),
        Line2D([], [], color=_INK_SECONDARY, marker="s", markersize=6,
               linestyle="none", label="no positive service"),
        Line2D([], [], color=_INK_SECONDARY, marker="x", markersize=7, linestyle="none",
               markeredgewidth=1.8, label="pre-bench failure"),
    ]
    labels += ["sustained estimate", "transient explore peak",
               "no positive service", "pre-bench failure"]

    fig.legend(handles, labels, loc="upper center", ncol=4,
               bbox_to_anchor=(0.5, 1.025), frameon=False, fontsize=12)
    fig.supxlabel("Iteration index", fontsize=13, color=_INK_SECONDARY)
    fig.supylabel("Goodput (successful req/s)", fontsize=13, color=_INK_SECONDARY)
    fig.tight_layout(rect=(0, 0, 1, 0.965))
    return _save(fig, out_dir, stem)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    agg = _REPO / "results_aggregate"
    ap.add_argument("--phases", type=Path, default=agg / "load_profile_phases.csv")
    ap.add_argument("--iterations", type=Path, default=agg / "iterations.csv")
    ap.add_argument("--failures", type=Path, default=agg / "failures.csv")
    ap.add_argument("--out-dir", type=Path, default=_REPO / "Writeup" / "figures" / "eval")
    ap.add_argument("--stem", default="goodput_trajectories_grid_annotated")
    a = ap.parse_args()

    pts, fails = build(a.phases, a.iterations, a.failures)
    n_hollow = int(((pts["sustained"] <= 0) & (pts["peak"] > 0)).sum())
    n_no_service = int(((pts["sustained"] <= 0) & ~(pts["peak"] > 0)).sum())
    print(f"points: {len(pts)} | sustained {int((pts['sustained'] > 0).sum())} | "
          f"transient {n_hollow} | no service {n_no_service} | "
          f"pre-bench failures {len(fails)}")
    for p in plot(pts, fails, a.out_dir, a.stem):
        print("wrote", p)


if __name__ == "__main__":
    main()

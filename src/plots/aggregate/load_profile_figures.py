"""
Figures for the load-profile evaluation RQ (Explore-and-Refine phase
behavior), built from ``results_aggregate/load_profile_phases.csv``
(``scripts/analysis/load_profile_phases.py``).

Reuses the palette/axis styling already established in :mod:`.figures` so
these plots read as part of the same figure set.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .figures import _AXIS, _INK_PRIMARY, _INK_SECONDARY, _save, _style_axes

_PHASE_COLORS = {
    "warm-up": "#898781",
    "explore": "#2a78d6",
    "recovery": "#eb6834",
    "refine": "#1baf7a",
}


def plot_phase_durations(
    df: pd.DataFrame, out_dir: Path, *, stem: str = "load_profile_phase_durations"
) -> list[Path]:
    """Box plot of per-phase wall-clock duration across all bench runs that
    survived warm-up (a run that fails warm-up never reaches explore/
    recovery/refine, so it contributes only to the warm-up box)."""
    phases = [
        ("warm-up", "warmup_duration_s"),
        ("explore", "explore_duration_s"),
        ("recovery", "recovery_duration_s"),
        ("refine", "refine_duration_s"),
    ]
    series = [df[col].dropna() for _, col in phases]
    if all(s.empty for s in series):
        raise ValueError("no phase-duration data to plot")

    fig, ax = plt.subplots(figsize=(6.0, 4.4))
    bp = ax.boxplot(
        series,
        positions=range(len(phases)),
        widths=0.5,
        showfliers=True,
        flierprops={"markersize": 3, "alpha": 0.35, "markeredgewidth": 0},
        patch_artist=True,
        medianprops={"color": _INK_PRIMARY, "linewidth": 1.6},
        whiskerprops={"color": _AXIS, "linewidth": 1.0},
        capprops={"color": _AXIS, "linewidth": 1.0},
    )
    for patch, (label, _) in zip(bp["boxes"], phases):
        patch.set(facecolor=_PHASE_COLORS[label], alpha=0.45, edgecolor=_PHASE_COLORS[label])

    ax.set_yscale("log")
    ax.set_xticks(range(len(phases)))
    ax.set_xticklabels([f"{label}\n(n={len(s)})" for (label, _), s in zip(phases, series)])
    ax.set_ylabel("Phase duration (s, log scale)")
    ax.set_title("Wall-clock duration by load-profile phase\n(one box run across all parsed bench runs)")
    _style_axes(ax)
    fig.tight_layout()
    return _save(fig, out_dir, stem)


def plot_peak_vs_sustained(
    df: pd.DataFrame, out_dir: Path, *, stem: str = "load_profile_peak_vs_sustained"
) -> list[Path]:
    """Scatter of explore-phase peak goodput vs. the refine-phase sustained
    estimate, one point per bench run that produced both quantities."""
    sub = df.dropna(subset=["explore_peak_goodput_rps", "sustained_goodput_rps"])
    sub = sub[sub["sustained_goodput_rps"] > 0]
    if sub.empty:
        raise ValueError("no runs with both an explore peak and a positive sustained estimate")

    x = sub["explore_peak_goodput_rps"].to_numpy()
    y = sub["sustained_goodput_rps"].to_numpy()
    above = y > x  # sustained exceeds the recorded explore peak

    fig, ax = plt.subplots(figsize=(5.2, 5.0))
    ax.scatter(
        x[~above], y[~above], s=14, alpha=0.45, color="#2a78d6",
        edgecolors="none", zorder=3, label="sustained < explore peak",
    )
    ax.scatter(
        x[above], y[above], s=14, alpha=0.55, color="#e34948",
        edgecolors="none", zorder=3, label="sustained > explore peak",
    )
    lo = min(x.min(), y.min()) * 0.8
    hi = max(x.max(), y.max()) * 1.2
    ax.plot([lo, hi], [lo, hi], color=_AXIS, linewidth=1.1, zorder=1, label="y = x")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("Explore-phase peak goodput (req/s, log scale)")
    ax.set_ylabel("Refine-phase sustained goodput (req/s, log scale)")
    ax.set_title(
        f"Explore peak vs. refined sustained estimate (n={len(sub)})\n"
        f"{100 * above.mean():.0f}% of runs settle above their own recorded explore peak"
    )
    _style_axes(ax)
    ax.legend(frameon=False, loc="upper left", fontsize=8)
    fig.tight_layout()
    return _save(fig, out_dir, stem)


def generate_load_profile_figures(df: pd.DataFrame, out_dir: Path) -> list[Path]:
    created: list[Path] = []
    for fn in (plot_phase_durations, plot_peak_vs_sustained):
        try:
            created.extend(fn(df, out_dir))
        except ValueError as exc:
            print(f"skip {fn.__name__}: {exc}")
    return created

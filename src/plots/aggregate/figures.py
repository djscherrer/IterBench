"""
Matplotlib figures for the cross-experiment tables built by :mod:`.aggregate`.

Color usage follows a fixed-order categorical palette (never cycled per-data)
so the same entity keeps the same color across figures:

- refinement kind (baseline/code/spec) reuses the palette already established
  by :mod:`.goodput_trajectory` for single-experiment plots;
- model and framework each get their own fixed slot assignment out of the
  validated 8-hue categorical order, assigned once in sorted order so adding
  a cell never repaints an existing series.

Status colors (good/warning/critical) are reserved for the completion-funnel
plot, where they denote actual run status, not an arbitrary 3rd category.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import MaxNLocator
import pandas as pd

from .tables import geometric_mean

# ---------------------------------------------------------------------------
# Palette (see docs/thesis figure palette note in this module's docstring)
# ---------------------------------------------------------------------------

_CATEGORICAL = [
    "#2a78d6",  # 1 blue
    "#008300",  # 2 green
    "#e87ba4",  # 3 magenta
    "#eda100",  # 4 yellow
    "#1baf7a",  # 5 aqua
    "#eb6834",  # 6 orange
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

_KIND_COLORS = {
    "baseline": "#6b7280",
    "code": "#2563eb",
    "spec": "#ea580c",
}

_ENV_COLORS = {
    "Go-net-http": "#2a78d6",
    "Python-Flask": "#eda100",
    "Rust-Actix": "#1baf7a",
}

_STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "critical": "#d03b3b",
}

_INK_PRIMARY = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_INK_MUTED = "#898781"
_GRIDLINE = "#e1e0d9"
_AXIS = "#c3c2b7"

plt.rcParams.update(
    {
        "font.size": 10,
        "axes.edgecolor": _AXIS,
        "axes.labelcolor": _INK_PRIMARY,
        "text.color": _INK_PRIMARY,
        "xtick.color": _INK_SECONDARY,
        "ytick.color": _INK_SECONDARY,
        "axes.titlecolor": _INK_PRIMARY,
        "grid.color": _GRIDLINE,
    }
)


def _model_colors(models: list[str]) -> dict[str, str]:
    return {m: _CATEGORICAL[i % len(_CATEGORICAL)] for i, m in enumerate(sorted(models))}


def _env_color(env: str, fallback_idx: int) -> str:
    return _ENV_COLORS.get(env, _CATEGORICAL[fallback_idx % len(_CATEGORICAL)])


def _short_model(name: str) -> str:
    return name.replace("openai-", "").replace("z-ai-", "").replace("deepseek-", "")


_SCENARIO_SHORT = {
    "BranchWeave_InteractiveStoryGraph": "BranchWeave",
    "ParcelPinLockerPickup": "ParcelPin",
    "SplitNestSharedExpenseLedger": "SplitNest",
    "TransitPulseDelayReporter": "TransitPulse",
}


def _short_scenario(name: str) -> str:
    return _SCENARIO_SHORT.get(name, name)


def _save(fig: plt.Figure, out_dir: Path, stem: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "pdf"):
        p = out_dir / f"{stem}.{ext}"
        fig.savefig(p, dpi=170, bbox_inches="tight")
        paths.append(p)
    plt.close(fig)
    return paths


def _style_axes(ax: plt.Axes) -> None:
    ax.grid(True, axis="y", linestyle="-", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(_AXIS)


# ---------------------------------------------------------------------------
# 1. Goodput trajectory small multiples: model comparison per scenario x env
# ---------------------------------------------------------------------------


def plot_goodput_trajectories_grid(
    iterations: pd.DataFrame, out_dir: Path, *, stem: str = "goodput_trajectories_grid"
) -> list[Path]:
    if iterations.empty:
        raise ValueError("no iteration rows to plot")

    scenarios = sorted(iterations["scenario"].unique())
    envs = sorted(iterations["env"].unique())
    models = sorted(iterations["model"].unique())
    colors = _model_colors(models)

    n_rows, n_cols = len(scenarios), len(envs)
    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(3.4 * n_cols, 2.12 * n_rows), squeeze=False, sharex=False
    )

    for r, scenario in enumerate(scenarios):
        for c, env in enumerate(envs):
            ax = axes[r][c]
            cell = iterations[(iterations["scenario"] == scenario) & (iterations["env"] == env)]
            if cell.empty:
                ax.axis("off")
                continue
            for model in models:
                sub = cell[cell["model"] == model].sort_values("iteration_index")
                if sub.empty:
                    continue
                ax.plot(
                    sub["iteration_index"],
                    sub["goodput_rps"],
                    color=colors[model],
                    linewidth=2.0,
                    marker="o",
                    markersize=4.5,
                    label=_short_model(model),
                )
            _style_axes(ax)
            top = ax.get_ylim()[1]
            ax.set_ylim(0, top if top > 1 else 1)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=8, min_n_ticks=6))
            if r == 0:
                ax.set_title(env, fontsize=14, fontweight="bold", color=_INK_PRIMARY)
            if c == 0:
                ax.set_ylabel(
                    _short_scenario(scenario), fontsize=13, fontweight="bold", color=_INK_PRIMARY
                )
            ax.tick_params(labelsize=11)

    handles, labels = [], []
    for model in models:
        (h,) = axes[0][0].plot([], [], color=colors[model], marker="o", markersize=6)
        handles.append(h)
        labels.append(_short_model(model))
    fig.legend(
        handles,
        labels,
        loc="upper center",
        ncol=len(models),
        bbox_to_anchor=(0.5, 1.02),
        frameon=False,
        fontsize=14,
    )
    fig.supxlabel("Iteration index", fontsize=13, color=_INK_SECONDARY)
    fig.supylabel("Sustained goodput (successful req/s)", fontsize=13, color=_INK_SECONDARY)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return _save(fig, out_dir, stem)


# ---------------------------------------------------------------------------
# 2. Baseline vs. best-iteration goodput, by model
# ---------------------------------------------------------------------------


def plot_baseline_vs_best_by_model(
    cells: pd.DataFrame, out_dir: Path, *, stem: str = "baseline_vs_best_by_model"
) -> list[Path]:
    from matplotlib.patches import Patch

    # Cells with no first-non-zero-goodput point never record positive
    # goodput at all; they have no valid gain reference and are excluded
    # from this comparison (Section 6.2 RQ1), not zero-filled.
    df = cells.dropna(subset=["first_nonzero_goodput_rps", "max_goodput_rps"])
    if df.empty:
        raise ValueError("no cells with both a first-non-zero and a max goodput")

    models = sorted(df["model"].unique())
    rng = np.random.default_rng(7)

    fig, ax = plt.subplots(figsize=(2.3 * len(models) + 1.5, 4.6))
    width = 0.32
    x = np.arange(len(models))

    box_style = dict(
        widths=width * 0.85,
        patch_artist=True,
        showfliers=False,
        whis=(0, 100),  # whiskers span the full min-max range: n is small (<=21) per box
        medianprops={"color": _INK_PRIMARY, "linewidth": 1.4},
        boxprops={"linewidth": 0.8, "edgecolor": _AXIS},
        whiskerprops={"color": _AXIS, "linewidth": 1.0},
        capprops={"color": _AXIS, "linewidth": 1.0},
    )

    for i, model in enumerate(models):
        sub = df[df["model"] == model]
        first_nonzero_vals = sub["first_nonzero_goodput_rps"].to_numpy()
        best_vals = sub["max_goodput_rps"].to_numpy()
        first_nonzero_gm = geometric_mean(list(first_nonzero_vals))
        best_gm = geometric_mean(list(best_vals))

        bp1 = ax.boxplot([first_nonzero_vals], positions=[x[i] - width / 2], **box_style)
        bp2 = ax.boxplot([best_vals], positions=[x[i] + width / 2], **box_style)
        for patch in bp1["boxes"]:
            patch.set_facecolor(_KIND_COLORS["baseline"])
            patch.set_alpha(0.5)
        for patch in bp2["boxes"]:
            patch.set_facecolor(_KIND_COLORS["code"])
            patch.set_alpha(0.5)

        jitter = rng.uniform(-0.06, 0.06, size=len(sub))
        ax.scatter(
            x[i] - width / 2 + jitter,
            first_nonzero_vals,
            color=_INK_PRIMARY,
            s=9,
            alpha=0.3,
            zorder=3,
            linewidths=0,
        )
        ax.scatter(
            x[i] + width / 2 + jitter,
            best_vals,
            color=_INK_PRIMARY,
            s=9,
            alpha=0.3,
            zorder=3,
            linewidths=0,
        )
        ax.scatter(
            [x[i] - width / 2],
            [first_nonzero_gm],
            marker="D",
            s=46,
            color=_KIND_COLORS["baseline"],
            edgecolors=_INK_PRIMARY,
            linewidths=0.9,
            zorder=5,
        )
        ax.scatter(
            [x[i] + width / 2],
            [best_gm],
            marker="D",
            s=46,
            color=_KIND_COLORS["code"],
            edgecolors=_INK_PRIMARY,
            linewidths=0.9,
            zorder=5,
        )

    ax.set_yscale("symlog", linthresh=100)
    ax.set_xticks(x)
    ax.set_xticklabels([_short_model(m) for m in models])
    ax.set_ylabel("Sustained goodput (successful req/s, symlog)")
    ax.set_title(
        "First-non-zero-goodput vs. best-refined goodput per model\n"
        "(box: quartiles and full range; diamond: geometric mean, as reported\n"
        "in the text; dots: individual scenario×framework cells)"
    )
    _style_axes(ax)
    legend_handles = [
        Patch(facecolor=_KIND_COLORS["baseline"], alpha=0.5, label="first non-zero-goodput iteration"),
        Patch(facecolor=_KIND_COLORS["code"], alpha=0.5, label="best refined iteration"),
        plt.Line2D(
            [0], [0], marker="D", linestyle="none", markersize=7,
            markerfacecolor="white", markeredgecolor=_INK_PRIMARY,
            label="geometric mean",
        ),
    ]
    ax.legend(handles=legend_handles, frameon=False, loc="upper left")
    fig.tight_layout()
    return _save(fig, out_dir, stem)


# ---------------------------------------------------------------------------
# 3. Framework comparison (max goodput reached), split by model
# ---------------------------------------------------------------------------


def plot_framework_comparison(
    cells: pd.DataFrame, out_dir: Path, *, stem: str = "framework_comparison"
) -> list[Path]:
    # A cell with max_goodput_rps == 0 never records positive goodput at
    # all and has no value a geometric mean can include (log(0) is
    # undefined); exclude it explicitly rather than let it silently drop
    # out via a NaN-only filter or, worse, get treated as a small value.
    df = cells.dropna(subset=["max_goodput_rps"])
    df = df[df["max_goodput_rps"] > 0]
    if df.empty:
        raise ValueError("no cells with positive max goodput")

    envs = sorted(df["env"].unique())
    models = sorted(df["model"].unique())
    model_colors = _model_colors(models)
    rng = np.random.default_rng(11)

    fig, ax = plt.subplots(figsize=(1.6 * len(envs) + 2.5, 4.5))
    group_width = 0.7
    n_models = max(len(models), 1)
    slot = group_width / n_models

    for gi, env in enumerate(envs):
        for mi, model in enumerate(models):
            sub = df[(df["env"] == env) & (df["model"] == model)]
            if sub.empty:
                continue
            xpos = gi - group_width / 2 + slot * (mi + 0.5)
            jitter = rng.uniform(-slot * 0.18, slot * 0.18, size=len(sub))
            ax.scatter(
                np.full(len(sub), xpos) + jitter,
                sub["max_goodput_rps"],
                color=model_colors[model],
                s=26,
                alpha=0.85,
                edgecolors="white",
                linewidths=0.5,
                label=_short_model(model) if gi == 0 else None,
                zorder=3,
            )
            ax.hlines(
                geometric_mean(list(sub["max_goodput_rps"])),
                xpos - slot * 0.32,
                xpos + slot * 0.32,
                color=model_colors[model],
                linewidth=2.2,
                zorder=4,
            )

    ax.set_yscale("symlog", linthresh=100)
    ax.set_xticks(range(len(envs)))
    ax.set_xticklabels(envs)
    ax.set_ylabel("Best goodput reached (successful req/s, symlog)")
    ax.set_title(
        "Best goodput reached per framework\n"
        "(dot: one scenario cell; bar: per-model geometric mean;\n"
        "cells with zero goodput throughout are excluded)"
    )
    _style_axes(ax)
    ax.legend(frameon=False, loc="center left", bbox_to_anchor=(1.01, 0.5))
    fig.tight_layout()
    return _save(fig, out_dir, stem)


# ---------------------------------------------------------------------------
# 4. Code vs. deployment (spec) refinement: marginal contribution per step
# ---------------------------------------------------------------------------


def plot_code_vs_spec_delta(
    iterations: pd.DataFrame, out_dir: Path, *, stem: str = "code_vs_spec_delta"
) -> list[Path]:
    df = iterations[iterations["refinement_kind"].isin(["code", "spec"])]
    df = df.dropna(subset=["delta_goodput_pct"])
    if df.empty:
        raise ValueError("no refinement steps with a defined percentage delta")

    kinds = ["code", "spec"]
    rng = np.random.default_rng(3)

    fig, ax = plt.subplots(figsize=(5.2, 4.5))
    lo, hi = df["delta_goodput_pct"].quantile([0.02, 0.98])
    pad = max(5.0, 0.1 * (hi - lo))

    for i, kind in enumerate(kinds):
        sub = df[df["refinement_kind"] == kind]["delta_goodput_pct"]
        if sub.empty:
            continue
        bp = ax.boxplot(
            sub,
            positions=[i],
            widths=0.45,
            showfliers=False,
            patch_artist=True,
            medianprops={"color": _INK_PRIMARY, "linewidth": 1.6},
        )
        for box in bp["boxes"]:
            box.set(facecolor=_KIND_COLORS[kind], alpha=0.35, edgecolor=_KIND_COLORS[kind])
        for whisker in bp["whiskers"]:
            whisker.set(color=_KIND_COLORS[kind])
        for cap in bp["caps"]:
            cap.set(color=_KIND_COLORS[kind])
        jitter = rng.uniform(-0.14, 0.14, size=len(sub))
        ax.scatter(
            np.full(len(sub), i) + jitter,
            sub,
            color=_KIND_COLORS[kind],
            s=14,
            alpha=0.5,
            linewidths=0,
            zorder=3,
        )

    ax.axhline(0.0, color=_AXIS, linewidth=1.0, zorder=1)
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xticks(range(len(kinds)))
    ax.set_xticklabels(["code refinement", "deployment (spec) refinement"])
    ax.set_ylabel("Δ goodput vs. previous successful iteration (%)")
    ax.set_title("Per-step goodput change by refinement lever\n(only steps where the previous iteration was already serving traffic)")
    _style_axes(ax)
    fig.tight_layout()
    return _save(fig, out_dir, stem)


# ---------------------------------------------------------------------------
# 5. Completion funnel per model
# ---------------------------------------------------------------------------


def plot_completion_funnel(
    cells: pd.DataFrame, out_dir: Path, *, stem: str = "completion_funnel", target_final_index: int = 10
) -> list[Path]:
    if cells.empty:
        raise ValueError("no cells to summarize")

    def _status(row: pd.Series) -> str:
        if not row["reached_baseline"]:
            return "aborted at baseline"
        if row["max_folder_index"] < target_final_index:
            return "progressed, ended early"
        return "completed full trajectory"

    df = cells.copy()
    df["status"] = df.apply(_status, axis=1)
    order = ["aborted at baseline", "progressed, ended early", "completed full trajectory"]
    colors = {
        "aborted at baseline": _STATUS["critical"],
        "progressed, ended early": _STATUS["warning"],
        "completed full trajectory": _STATUS["good"],
    }

    models = sorted(df["model"].unique())
    counts = df.groupby(["model", "status"]).size().unstack(fill_value=0)
    for status in order:
        if status not in counts.columns:
            counts[status] = 0
    counts = counts[order]

    fig, ax = plt.subplots(figsize=(1.6 * len(models) + 2.0, 4.2))
    x = np.arange(len(models))
    bottom = np.zeros(len(models))
    for status in order:
        vals = counts.loc[models, status].to_numpy(dtype=float)
        ax.bar(x, vals, bottom=bottom, color=colors[status], label=status, width=0.55)
        for xi, (v, b) in enumerate(zip(vals, bottom)):
            if v > 0:
                ax.text(
                    x[xi],
                    b + v / 2,
                    str(int(v)),
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white" if status != "progressed, ended early" else _INK_PRIMARY,
                )
        bottom += vals

    ax.set_xticks(x)
    ax.set_xticklabels([_short_model(m) for m in models])
    ax.set_ylabel("Number of scenario×framework cells")
    ax.set_title("Run completion status per model")
    _style_axes(ax)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=1)
    fig.tight_layout()
    return _save(fig, out_dir, stem)


# ---------------------------------------------------------------------------
# 6. Failure taxonomy
# ---------------------------------------------------------------------------


def plot_failure_taxonomy(
    failures: pd.DataFrame, out_dir: Path, *, stem: str = "failure_taxonomy", top_n: int = 15
) -> list[Path]:
    if failures.empty:
        raise ValueError("no failure records to plot")

    phases = ["01-decision", "02-code", "03-spec", "04-deploy", "05-bench"]
    phase_colors = {p: _CATEGORICAL[i % len(_CATEGORICAL)] for i, p in enumerate(phases)}

    grouped = (
        failures.groupby(["phase", "failure_kind"]).size().reset_index(name="count").sort_values("count", ascending=False)
    )
    grouped = grouped.head(top_n).iloc[::-1]
    labels = [f"{row.phase.split('-', 1)[-1]}: {row.failure_kind}" for row in grouped.itertuples()]

    fig, ax = plt.subplots(figsize=(6.5, 0.34 * len(grouped) + 1.4))
    colors = [phase_colors.get(p, _INK_MUTED) for p in grouped["phase"]]
    ax.barh(labels, grouped["count"], color=colors)
    for y, v in enumerate(grouped["count"]):
        ax.text(v + max(grouped["count"]) * 0.01, y, str(int(v)), va="center", fontsize=8, color=_INK_SECONDARY)

    ax.set_xlabel("Occurrences across all iterations")
    ax.set_title(f"Top {len(grouped)} failure kinds by phase")
    _style_axes(ax)
    ax.grid(True, axis="x", linestyle="-", linewidth=0.6, alpha=0.7)
    ax.grid(False, axis="y")
    fig.tight_layout()
    return _save(fig, out_dir, stem)


def generate_all_figures(data, out_dir: Path) -> list[Path]:
    """Best-effort: generate every figure, skipping ones with insufficient data."""
    from .tables import AggregateData

    assert isinstance(data, AggregateData)
    created: list[Path] = []
    steps = [
        (plot_goodput_trajectories_grid, data.iterations),
        (plot_baseline_vs_best_by_model, data.cells),
        (plot_framework_comparison, data.cells),
        (plot_code_vs_spec_delta, data.iterations),
        (plot_completion_funnel, data.cells),
        (plot_failure_taxonomy, data.failures),
    ]
    for fn, df in steps:
        try:
            created.extend(fn(df, out_dir))
        except ValueError as exc:
            print(f"skip {fn.__name__}: {exc}")
    return created

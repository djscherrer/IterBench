from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.transforms import blended_transform_factory

from ..workspace import PLOTS_DIRNAME
from .ramp_data import (
    AdaptiveDecision,
    AdaptivePlotParams,
    AdaptiveRunOutcome,
    GoodputTimeline,
    LatencySample,
    LoadPhaseSpan,
    PeakGoodputMarker,
    P95Timeline,
    anchor_users_at_decision,
    classify_bench_run_outcome,
    format_decision_tuple,
    gather_bench_log_text,
    group_goodput_sample_segments,
    group_latency_sample_segments,
    load_adaptive_plot_params,
    load_stats_timeseries,
    parse_adaptive_decisions,
    parse_controller_goodput_timeline,
    parse_controller_p95_timeline,
    parse_explore_refine_phase_spans,
    plottable_decisions,
    resolve_run_goodput_marker,
    is_explore_refine_bench,
)

ADAPTIVE_RAMP_PLOT_FILENAME = "adaptive_ramp.png"
#
# Note: Locust's stats_history throughput columns (Requests/s, Failures/s)
# are already based on a trailing rolling window (see Locust's current_rps).
# We intentionally do not apply any additional smoothing in this plot.

_COLOR_GOODPUT = "#16a34a"
_COLOR_GOODPUT_LIGHT = "#86efac"
_COLOR_REQ = "#ea580c"
_COLOR_FAIL = "#dc2626"
_COLOR_USERS = "#2563eb"
_COLOR_P95 = "#9333ea"
_COLOR_DECISION = "#64748b"
_COLOR_PEAK = "#dc2626"
_COLOR_TRIM_SHADE = "#f59e0b"  # darker amber/yellow
_TRIM_SHADE_ALPHA = 0.20

_PHASE_STYLES: dict[str, dict[str, str]] = {
    "warmup": {"fill": "#e0e7ff", "edge": "#6366f1", "label": "Warmup"},
    "explore": {"fill": "#dbeafe", "edge": "#2563eb", "label": "Explore"},
    "recovery": {"fill": "#fef3c7", "edge": "#d97706", "label": "Recovery"},
    "refine": {"fill": "#dcfce7", "edge": "#16a34a", "label": "Refine"},
}
_PHASE_FILL_ALPHA = 0.22

_DECISION_BBOX = {
    "boxstyle": "round,pad=0.3",
    "facecolor": "white",
    "edgecolor": "#cbd5e1",
    "alpha": 0.92,
}

_DECISION_BOX_FONTSIZE = 8.5
_DECISION_FORMAT_FONTSIZE = 8


def plot_adaptive_ramp(
    bench_dir: Path,
    *,
    out_dir: Path | None = None,
    show: bool = False,
) -> Path:
    """
    Adaptive ramp plot for one ``05-bench/`` directory.

    Primary Y: virtual users + throughput series (rolling mean).
    Secondary Y: per-step controller P95 (same metric as decision boxes).
    Decisions: compact tuple boxes above each decision vertical line.
    """
    bench_dir = bench_dir.expanduser().resolve()
    plot_params = load_adaptive_plot_params(bench_dir)
    df = load_stats_timeseries(bench_dir)
    log_text = gather_bench_log_text(bench_dir)
    decisions = parse_adaptive_decisions(log_text)
    panel_decisions = plottable_decisions(decisions)
    p95_timeline = parse_controller_p95_timeline(
        log_text,
        decisions,
        trim_s=plot_params.trim_s,
        sample_every_s=plot_params.sample_every_s,
        min_settle_samples=plot_params.min_settle_samples,
    )
    goodput_timeline = parse_controller_goodput_timeline(
        log_text,
        decisions,
        trim_s=plot_params.trim_s,
        sample_every_s=plot_params.sample_every_s,
        min_settle_samples=plot_params.min_settle_samples,
    )
    peak = resolve_run_goodput_marker(bench_dir, log_text, decisions)
    explore_refine = is_explore_refine_bench(bench_dir)
    run_outcome = classify_bench_run_outcome(bench_dir, log_text) if explore_refine else None
    phase_spans = parse_explore_refine_phase_spans(
        log_text,
        t_end_s=float(df["t_s"].max()),
    )

    df = df.copy()
    df["goodput_plot"] = df["goodput_rps"]
    df["req_plot"] = df["req_rps"]
    df["fail_plot"] = df["fail_rps"]
    df["users_plot"] = df["users"]

    plots_dir = out_dir or (bench_dir / PLOTS_DIRNAME)
    plots_dir.mkdir(parents=True, exist_ok=True)
    out_path = plots_dir / ADAPTIVE_RAMP_PLOT_FILENAME

    fig, ax = plt.subplots(figsize=(14, 6.5))
    ax_p95 = ax.twinx()

    if phase_spans:
        _add_phase_regions(ax, phase_spans)

    ax.plot(
        df["t_s"],
        df["users_plot"],
        color=_COLOR_USERS,
        linewidth=2.0,
        alpha=0.85,
        label="Virtual users",
        zorder=4,
    )
    ax.plot(
        df["t_s"],
        df["goodput_plot"],
        color=_COLOR_GOODPUT_LIGHT,
        linewidth=2.0,
        label="Goodput (Locust stats, ~10s rolling)",
        zorder=3,
    )
    ax.plot(
        df["t_s"],
        df["req_plot"],
        color=_COLOR_REQ,
        linewidth=1.6,
        alpha=0.85,
        linestyle=":",
        label="Total req/s (Locust stats, ~10s rolling)",
        zorder=2,
    )
    ax.plot(
        df["t_s"],
        df["fail_plot"],
        color=_COLOR_FAIL,
        linewidth=1.4,
        alpha=0.9,
        label="Failures/s (Locust stats, ~10s rolling)",
        zorder=2,
    )
    p95_has_data = bool(p95_timeline.all_samples)
    if p95_has_data:
        _plot_p95_timeline(ax_p95, p95_timeline, plot_params=plot_params)
    if goodput_timeline.has_full_timeline:
        _plot_goodput_samples(ax, goodput_timeline, plot_params=plot_params)

    _add_trim_shading(ax, decisions, plot_params=plot_params)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Virtual users / throughput (req/s)")
    if p95_has_data:
        ax_p95.set_ylabel("P95 latency (ms)", color=_COLOR_P95)
        ax_p95.tick_params(axis="y", labelcolor=_COLOR_P95)
        p95_ymin, p95_ymax = _p95_axis_limits(
            p95_timeline,
            panel_decisions,
            sla_ms=plot_params.sla_ms,
        )
        ax_p95.set_ylim(p95_ymin, p95_ymax)
        if plot_params.sla_ms > 0:
            _add_sla_latency_marker(
                ax_p95,
                t_end_s=float(df["t_s"].max()),
                sla_ms=plot_params.sla_ms,
            )

    y_max = max(
        df["users_plot"].max(),
        df["goodput_plot"].max(),
        df["req_plot"].max(),
        1.0,
    )
    ax.set_ylim(0, y_max * 1.12)

    _add_decision_markers(ax, panel_decisions, y_max=y_max)
    _add_decision_boxes_above_plot(ax, panel_decisions)
    if p95_has_data:
        _add_p95_decision_anchors(ax, panel_decisions)
    _add_peak_goodput_marker(
        ax,
        peak,
        df,
        refine_phase_only=explore_refine,
        underestimate=bool(run_outcome.underestimate) if run_outcome else False,
    )
    if run_outcome is not None:
        _add_run_outcome_notice(ax, run_outcome)

    ax.set_title(
        f"Adaptive load ramp — {bench_dir.parent.name}\n"
        f"(goodput = successful req/s over Locust's ~10s rolling window; "
        f"{_p95_subtitle(plot_params)})"
    )
    ax.grid(True, linestyle=":", alpha=0.4)

    handles, labels = ax.get_legend_handles_labels()
    if p95_has_data:
        handles_p95, labels_p95 = ax_p95.get_legend_handles_labels()
        handles += handles_p95
        labels += labels_p95
    handles.append(Patch(facecolor=_COLOR_TRIM_SHADE, edgecolor="none", alpha=_TRIM_SHADE_ALPHA))
    labels.append(f"Trim window ({plot_params.trim_s}s, ignored for decisions)")
    if phase_spans:
        for span in phase_spans:
            style = _PHASE_STYLES[span.name]
            handles.append(
                Patch(
                    facecolor=style["fill"],
                    edgecolor=style["edge"],
                    alpha=_PHASE_FILL_ALPHA,
                    linewidth=1.0,
                )
            )
            labels.append(
                f"{style['label']} ({int(span.t_start)}–{int(span.t_end)}s)"
            )
    if peak is not None:
        peak_label = (
            "Sustained max goodput (refine phase)"
            if explore_refine
            else "Sustained max goodput"
        )
        if run_outcome is not None and run_outcome.underestimate:
            peak_label += " — underestimate"
        peak_handle = Line2D(
            [0],
            [0],
            marker="x",
            color=_COLOR_PEAK,
            linestyle="None",
            markersize=8,
            markeredgewidth=2,
            label=peak_label,
        )
        legend_handles = handles + [peak_handle]
        legend_labels = labels + [peak_label]
    elif run_outcome is not None and not run_outcome.refine_reached:
        peak_handle = Line2D(
            [0],
            [0],
            marker="",
            color=_COLOR_PEAK,
            linestyle="None",
            label=run_outcome.title,
        )
        legend_handles = handles + [peak_handle]
        legend_labels = labels + [run_outcome.title]
    else:
        legend_handles = handles
        legend_labels = labels
    legend = ax.legend(
        legend_handles,
        legend_labels,
        loc="upper left",
        fontsize=8,
    )
    _draw_decision_format_hint(fig, ax, legend)

    fig.subplots_adjust(bottom=0.20 if phase_spans else 0.16)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)
    return out_path


def _add_phase_regions(ax: plt.Axes, phases: tuple[LoadPhaseSpan, ...]) -> None:
    """Shade explore-refine controller phases and mark boundaries."""
    if not phases:
        return

    trans = blended_transform_factory(ax.transData, ax.transAxes)
    drawn_edges: set[int] = set()

    for span in phases:
        style = _PHASE_STYLES.get(span.name)
        if style is None:
            continue
        if span.t_end > span.t_start:
            ax.axvspan(
                span.t_start,
                span.t_end,
                facecolor=style["fill"],
                edgecolor="none",
                alpha=_PHASE_FILL_ALPHA,
                zorder=0,
            )
        start_i = int(round(span.t_start))
        if start_i not in drawn_edges:
            drawn_edges.add(start_i)
            ax.axvline(
                span.t_start,
                color=style["edge"],
                linewidth=1.2,
                linestyle="--",
                alpha=0.75,
                zorder=1,
            )
        mid_t = (span.t_start + span.t_end) / 2.0
        if span.t_end > span.t_start:
            ax.text(
                mid_t,
                -0.045,
                style["label"],
                transform=trans,
                ha="center",
                va="top",
                fontsize=8,
                color=style["edge"],
                fontweight="bold",
                alpha=0.9,
                zorder=6,
                clip_on=False,
            )

    last = phases[-1]
    end_i = int(round(last.t_end))
    if end_i not in drawn_edges and last.t_end > 0:
        ax.axvline(
            last.t_end,
            color=_PHASE_STYLES[last.name]["edge"],
            linewidth=1.2,
            linestyle="--",
            alpha=0.75,
            zorder=1,
        )


def _add_trim_shading(
    ax: plt.Axes,
    decisions: list[AdaptiveDecision],
    *,
    plot_params: AdaptivePlotParams,
) -> None:
    """Shade per-level trim windows (controller ignores these for decisions)."""
    trim_s = max(0, int(plot_params.trim_s))
    if trim_s <= 0 or not decisions:
        return

    # We treat "warmup end" as the first level start; each subsequent level
    # starts at the previous phase-end decision timestamp.
    ordered = sorted(decisions, key=lambda d: d.t_s)
    warmup = next((d for d in ordered if d.label == "warmup end"), None)
    phases = [d for d in ordered if d.label != "warmup end"]
    if not phases:
        return

    for i, dec in enumerate(phases):
        level_start = warmup.t_s if warmup and i == 0 else phases[i - 1].t_s
        ax.axvspan(
            level_start,
            level_start + trim_s,
            facecolor=_COLOR_TRIM_SHADE,
            edgecolor="none",
            alpha=_TRIM_SHADE_ALPHA,
            zorder=0,
        )

def _p95_axis_limits(
    timeline: P95Timeline,
    decisions: list[AdaptiveDecision],
    *,
    sla_ms: float,
) -> tuple[float, float]:
    """Fit the P95 axis to observed controller samples and decision values."""
    values: list[float] = [s.p95_ms for s in timeline.all_samples]
    values.extend(
        d.p95_ms for d in decisions if d.p95_ms is not None and d.p95_ms > 0
    )
    if sla_ms > 0:
        values.append(float(sla_ms))
    if not values:
        return 0.0, 450.0

    y_min = min(values)
    y_max = max(values)
    span = max(y_max - y_min, y_max * 0.05, 20.0)
    pad = max(20.0, span * 0.1)
    return max(0.0, y_min - pad * 0.25), y_max + pad


def _draw_decision_format_hint(
    fig: plt.Figure,
    ax: plt.Axes,
    legend: plt.Legend,
) -> None:
    """Format key for decision boxes, aligned below the series legend."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bbox = legend.get_window_extent(renderer=renderer).transformed(
        ax.transAxes.inverted()
    )
    ax.text(
        bbox.x0,
        bbox.y0 - 0.008,
        "Decisions\n  (goodput, err%)\n  @users → Δusers",
        transform=ax.transAxes,
        fontsize=_DECISION_FORMAT_FONTSIZE,
        va="top",
        ha="left",
        family="monospace",
        linespacing=1.2,
        bbox=_DECISION_BBOX,
        zorder=5,
    )


def _add_decision_boxes_above_plot(
    ax: plt.Axes,
    decisions: list[AdaptiveDecision],
) -> None:
    """Compact tuple boxes centered above the plot at each decision time."""
    if not decisions:
        return

    trans = blended_transform_factory(ax.transData, ax.transAxes)
    # Stagger vertically when decision times are close together.
    last_t: int | None = None
    level = 0
    for decision in decisions:
        if last_t is not None and decision.t_s - last_t < 20:
            level = (level + 1) % 3
        else:
            level = 0
        last_t = decision.t_s

        line1, line2, line3 = format_decision_tuple(decision)
        y_axes = 0.97 - level * 0.075
        ax.text(
            decision.t_s,
            y_axes,
            f"{line1}\n{line2}\n{line3}",
            transform=trans,
            ha="center",
            va="top",
            fontsize=_DECISION_BOX_FONTSIZE,
            family="monospace",
            linespacing=1.15,
            bbox=_DECISION_BBOX,
            zorder=5,
            clip_on=False,
        )


def _add_decision_markers(
    ax: plt.Axes,
    decisions: list[AdaptiveDecision],
    *,
    y_max: float,
) -> None:
    """Vertical time markers at each decision."""
    marked_times: list[int] = []
    for decision in decisions:
        t_s = decision.t_s
        if t_s in marked_times:
            continue
        marked_times.append(t_s)

        ax.axvline(
            t_s,
            color=_COLOR_DECISION,
            linewidth=0.9,
            linestyle=":",
            alpha=0.7,
            zorder=1,
        )
        ax.text(
            t_s,
            -y_max * 0.06,
            f"{t_s}s",
            ha="center",
            va="top",
            fontsize=7,
            color=_COLOR_DECISION,
            clip_on=False,
        )


def _plot_latency_segments(
    ax_p95: plt.Axes,
    samples: list[LatencySample],
    *,
    color: str,
    linewidth: float,
    alpha: float,
    zorder: int,
) -> None:
    for segment in group_latency_sample_segments(samples):
        xs = [s.t_s for s in segment]
        ys = [s.p95_ms for s in segment]
        ax_p95.plot(
            xs,
            ys,
            color=color,
            linewidth=linewidth,
            linestyle="-",
            alpha=alpha,
            zorder=zorder,
        )


def _p95_subtitle(params: AdaptivePlotParams) -> str:
    return (
        f"P95 = controller samples from Locust trailing ~10s window "
        f"(every {params.sample_every_s}s after level settle)"
    )


def _plot_p95_timeline(
    ax_p95: plt.Axes,
    timeline: P95Timeline,
    *,
    plot_params: AdaptivePlotParams,
) -> None:
    """Plot every controller P95 sample with the same metric and style."""
    samples = list(timeline.all_samples)
    if not samples:
        return

    _plot_latency_segments(
        ax_p95,
        samples,
        color=_COLOR_P95,
        linewidth=1.2,
        alpha=0.35,
        zorder=1,
    )
    ax_p95.scatter(
        [s.t_s for s in samples],
        [s.p95_ms for s in samples],
        color=_COLOR_P95,
        s=12,
        alpha=0.25,
        zorder=2,
        clip_on=False,
    )
    ax_p95.plot(
        [],
        [],
        color=_COLOR_P95,
        marker="o",
        linestyle="-",
        linewidth=1.2,
        markersize=4,
        label="P95 samples (observe only)",
    )


def _plot_goodput_samples(
    ax: plt.Axes,
    timeline: GoodputTimeline,
    *,
    plot_params: AdaptivePlotParams,
) -> None:
    all_samples = list(timeline.all_samples)
    decision_samples = list(timeline.decision_samples)
    if not all_samples:
        return

    decision_set = set(decision_samples)
    observe = [s for s in all_samples if s not in decision_set]

    if observe:
        for seg in group_goodput_sample_segments(observe):
            ax.plot(
                [s.t_s for s in seg],
                [s.goodput_rps for s in seg],
                color=_COLOR_GOODPUT_LIGHT,
                linewidth=1.2,
                alpha=0.35,
                zorder=1,
            )
        ax.scatter(
            [s.t_s for s in observe],
            [s.goodput_rps for s in observe],
            color=_COLOR_GOODPUT_LIGHT,
            s=10,
            alpha=0.35,
            zorder=2,
            clip_on=False,
        )
        ax.plot(
            [],
            [],
            color=_COLOR_GOODPUT_LIGHT,
            marker="o",
            linestyle="None",
            markersize=4,
            label="Goodput samples (observe only)",
        )

    for seg in group_goodput_sample_segments(decision_samples):
        ax.plot(
            [s.t_s for s in seg],
            [s.goodput_rps for s in seg],
            color=_COLOR_GOODPUT,
            linewidth=1.8,
            alpha=0.85,
            zorder=3,
        )
    ax.scatter(
        [s.t_s for s in decision_samples],
        [s.goodput_rps for s in decision_samples],
        color=_COLOR_GOODPUT,
        s=18,
        alpha=0.9,
        zorder=4,
        clip_on=False,
    )
    n = plot_params.min_settle_samples
    window_s = n * plot_params.sample_every_s
    ax.plot(
        [],
        [],
        color=_COLOR_GOODPUT,
        marker="o",
        linestyle="None",
        markersize=5,
        label=f"Goodput samples (~{window_s:.0f}s rolling window)",
    )

def _add_p95_decision_anchors(
    ax: plt.Axes,
    decisions: list[AdaptiveDecision],
) -> None:
    """
    P95 + user-level anchors below the plot at each decision time.

    Anchors the violet step line to the controller values from bench logs.
    """
    if not decisions:
        return

    trans = blended_transform_factory(ax.transData, ax.transAxes)
    last_t: int | None = None
    level = 0
    for decision in decisions:
        if decision.p95_ms is None:
            continue
        if last_t is not None and decision.t_s - last_t < 20:
            level = (level + 1) % 2
        else:
            level = 0
        last_t = decision.t_s

        users = anchor_users_at_decision(decision)
        line1 = f"{decision.p95_ms:.0f}ms"
        line2 = f"@{users}u" if users is not None else "—"
        y_axes = -0.06 - level * 0.055
        ax.text(
            decision.t_s,
            y_axes,
            f"{line1}\n{line2}",
            transform=trans,
            ha="center",
            va="top",
            fontsize=7,
            color=_COLOR_P95,
            family="monospace",
            linespacing=1.1,
            clip_on=False,
            zorder=5,
        )


def _add_sla_latency_marker(
    ax_p95: plt.Axes,
    *,
    t_end_s: float,
    sla_ms: float,
) -> None:
    """Horizontal SLA reference on the P95 axis."""
    ax_p95.axhline(
        sla_ms,
        color=_COLOR_DECISION,
        linewidth=0.9,
        linestyle=":",
        alpha=0.7,
        zorder=2,
    )
    ax_p95.annotate(
        f"SLA {sla_ms:.0f}ms",
        xy=(t_end_s, sla_ms),
        xytext=(6, 0),
        textcoords="offset points",
        ha="left",
        va="center",
        fontsize=7,
        color=_COLOR_DECISION,
        clip_on=False,
        zorder=5,
    )


def _add_peak_goodput_marker(
    ax: plt.Axes,
    peak: PeakGoodputMarker | None,
    df: pd.DataFrame,
    *,
    refine_phase_only: bool = False,
    underestimate: bool = False,
) -> None:
    """
    Mark the sustained max goodput window on the smoothed goodput curve.

    Uses the rolling-window metric from ``stats_history`` (same as experiment
    trajectory plots). The cross sits on the smoothed goodput curve; the label
    shows the sustained window average.
    """
    if peak is None:
        return
    nearest_idx = (df["t_s"] - peak.t_s).abs().idxmin()
    # Plot the marker on the Locust stats goodput curve (10s rolling).
    y_on_curve = float(df.loc[nearest_idx, "goodput_plot"])
    ax.scatter(
        peak.t_s,
        y_on_curve,
        marker="x",
        s=90,
        color=_COLOR_PEAK,
        linewidths=2.5,
        zorder=6,
    )
    if refine_phase_only:
        label = f"sustained {peak.goodput_rps:.0f}/s (refine)"
    else:
        label = f"sustained {peak.goodput_rps:.0f}/s"
    if underestimate:
        label += " — underestimate"
    ax.annotate(
        label,
        (peak.t_s, y_on_curve),
        textcoords="offset points",
        xytext=(6, 6),
        fontsize=7,
        color=_COLOR_PEAK,
        fontweight="bold",
        ha="left",
        va="bottom",
    )


def _add_run_outcome_notice(ax: plt.Axes, outcome: AdaptiveRunOutcome) -> None:
    """Show classified run outcome in the lower-right red box."""
    ax.text(
        0.99,
        0.02,
        outcome.plot_box_text(),
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        color=_COLOR_PEAK,
        fontstyle="italic",
        bbox={
            "boxstyle": "round,pad=0.35",
            "facecolor": "white",
            "edgecolor": _COLOR_PEAK,
            "alpha": 0.92,
            "linewidth": 0.8,
        },
        zorder=7,
    )


def regenerate_bench_plots(bench_dir: Path) -> list[Path]:
    """Regenerate all plots for one ``05-bench/`` directory."""
    created: list[Path] = []
    try:
        created.append(plot_adaptive_ramp(bench_dir))
    except (FileNotFoundError, ValueError):
        pass
    return created

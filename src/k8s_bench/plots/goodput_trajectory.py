from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt

from ..workspace import PLOTS_DIRNAME
from .data import (
    IterationGoodputPoint,
    collect_iteration_goodput_points,
    resolve_experiment_root,
)

GOODPUT_PLOT_FILENAME = "goodput_per_iteration.png"
GOODPUT_DATA_FILENAME = "goodput_per_iteration.json"

_COLOR_BASELINE = "#6b7280"
_COLOR_CODE = "#2563eb"
_COLOR_SPEC = "#ea580c"


def _style_for_kind(kind: str) -> dict[str, object]:
    if kind == "code":
        return {
            "color": _COLOR_CODE,
            "marker": "o",
            "label": "code refinement",
        }
    if kind == "spec":
        return {
            "color": _COLOR_SPEC,
            "marker": "x",
            "label": "spec refinement",
        }
    return {
        "color": _COLOR_BASELINE,
        "marker": "o",
        "label": "baseline",
    }


def plot_goodput_per_iteration(
    experiment_root: Path,
    *,
    out_dir: Path | None = None,
    show: bool = False,
) -> Path:
    """
    Plot peak goodput (succ/s) per iteration for one k8s experiment.

    Writes ``plots/goodput_per_iteration.png`` next to ``iterations/``.
    """
    root = resolve_experiment_root(experiment_root)
    points = collect_iteration_goodput_points(root)
    if not points:
        raise ValueError(f"No completed benchmark iterations found under {root}")

    plots_dir = out_dir or (root / PLOTS_DIRNAME)
    plots_dir.mkdir(parents=True, exist_ok=True)
    out_path = plots_dir / GOODPUT_PLOT_FILENAME

    fig, ax = plt.subplots(figsize=(10, 5.5))
    x_vals = [p.iteration_index for p in points]
    y_vals = [p.goodput_rps for p in points]

    ax.plot(
        x_vals,
        y_vals,
        color="#94a3b8",
        linewidth=1.5,
        linestyle="--",
        marker="",
        zorder=1,
        alpha=0.8,
    )

    seen_labels: set[str] = set()
    for point in points:
        style = _style_for_kind(point.refinement_kind)
        label = str(style["label"])
        if label in seen_labels:
            label = ""
        else:
            seen_labels.add(str(style["label"]))
        ax.scatter(
            point.iteration_index,
            point.goodput_rps,
            color=style["color"],
            marker=style["marker"],
            s=90,
            linewidths=2.0 if point.refinement_kind == "spec" else 1.0,
            label=label,
            zorder=3,
        )
        label = f"{point.goodput_rps:.0f}"
        if point.users_at_peak is not None:
            label = f"{label}\n@{point.users_at_peak}u"
        ax.annotate(
            label,
            (point.iteration_index, point.goodput_rps),
            textcoords="offset points",
            xytext=(0, 10),
            ha="center",
            fontsize=8,
            color=str(style["color"]),
        )

    ax.set_xlabel("Iteration")
    ax.set_ylabel("Sustained max goodput (successful req/s)")
    ax.set_title(f"Sustained goodput trajectory — {root.name}")
    ax.grid(True, linestyle=":", alpha=0.5)
    ax.set_xticks(x_vals)
    ax.set_xticklabels([str(x) for x in x_vals])
    if points:
        y_min = min(y_vals)
        y_max = max(y_vals)
        pad = max(20.0, (y_max - y_min) * 0.1)
        ax.set_ylim(max(0.0, y_min - pad), y_max + pad)
    ax.legend(loc="best", frameon=True)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)

    _write_plot_data(plots_dir, root, points)
    return out_path


def _write_plot_data(
    plots_dir: Path,
    experiment_root: Path,
    points: list[IterationGoodputPoint],
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment_root": str(experiment_root),
        "points": [
            {
                "iteration_index": p.iteration_index,
                "iteration_id": p.iteration_id,
                "folder_name": p.folder_name,
                "refinement_kind": p.refinement_kind,
                "goodput_rps": p.goodput_rps,
                "users_at_peak": p.users_at_peak,
                "final_users": p.final_users,
                "goodput_history": [
                    {"users": users, "goodput_rps": goodput}
                    for users, goodput in p.goodput_history
                ],
            }
            for p in points
        ],
    }
    (plots_dir / GOODPUT_DATA_FILENAME).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def regenerate_experiment_plots(
    experiment_root: Path,
    *,
    include_bench_plots: bool = True,
) -> list[Path]:
    """Regenerate experiment-level plots and optionally all per-bench plots."""
    from ..workspace import (
        ITERATIONS_DIRNAME,
        bench_dir_has_complete_run,
        iteration_bench_dir,
        iteration_folder_is_failed,
        parse_iteration_index,
    )
    from .adaptive_ramp import regenerate_bench_plots

    root = resolve_experiment_root(experiment_root)
    created: list[Path] = []
    try:
        created.append(plot_goodput_per_iteration(root))
    except ValueError:
        pass

    if include_bench_plots:
        iterations_dir = root / ITERATIONS_DIRNAME
        if iterations_dir.is_dir():
            for child in sorted(iterations_dir.iterdir()):
                if not child.is_dir() or iteration_folder_is_failed(child.name):
                    continue
                if parse_iteration_index(child.name) is None:
                    continue
                bench_dir = iteration_bench_dir(child)
                if bench_dir_has_complete_run(bench_dir):
                    created.extend(regenerate_bench_plots(bench_dir))
    return created

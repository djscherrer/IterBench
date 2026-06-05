import logging
import os
import pathlib
from typing import Optional, Tuple

import matplotlib.cm as cm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import colormaps
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize


def plot_requests_vs_percentile(
    csv_path: str,
    x_col: str = "Requests/s",
    x_col2: str = "Failures/s",
    y_col: str = "99%",  # any percentile column, e.g. "95%", "99.9%", etc.
    name_col: str = "Name",
    name_value: str = "Aggregated",
    decreasing_run: int = 5,  # consecutive strictly-decreasing points to trigger cutoff
    cutoff_delta: int = 0,  # keep rows up to (start_index_of_run + cutoff_delta), inclusive
    ax: Optional[
        plt.Axes
    ] = None,  # pass an existing axes to draw on, or leave None to create one
    **plot_kwargs,  # e.g. linewidth=2, marker="o"
) -> Tuple[plt.Axes, pd.DataFrame]:
    """
    Read a CSV of load-test stats and plot y_col vs x_col for rows where name_col == name_value.
    Additionally, if x_col strictly decreases for `decreasing_run` consecutive rows, drop all rows
    AFTER (start_index_of_run + cutoff_delta).

    Parameters
    ----------
    csv_path : str
        Path to the CSV file.
    x_col : str
        Column name to use for the x-axis (default: "Requests/s").
    y_col : str
        Column name to use for the y-axis (default: "99%").
    name_col : str
        Column that identifies series/groups (default: "Name").
    name_value : str
        Required value in name_col to keep (default: "Aggregated").
    decreasing_run : int
        Length of a strictly decreasing run in x_col that triggers cutoff (default: 5).
    cutoff_delta : int
        Keep rows up to (start_index_of_run + cutoff_delta), inclusive (default: 0).
    ax : matplotlib.axes.Axes or None
        Existing axes to plot on; if None, a new figure/axes is created.
    **plot_kwargs :
        Passed through to `ax.plot(...)`.

    Returns
    -------
    ax : matplotlib.axes.Axes
        The axes the line was drawn on.
    df_used : pandas.DataFrame
        The filtered DataFrame that was actually plotted (after cutoff & NaN removal).

    Notes
    -----
    - The CSV may contain non-numeric cells; this coerces x_col and y_col to numeric.
    - Cutoff uses the *first* occurrence of a strictly-decreasing run of the requested length.
    - Comparison is strict: x[i] > x[i+1] > ... > x[i+decreasing_run-1].
    """
    # Read & filter
    df = pd.read_csv(csv_path)
    df = df[df[name_col] == name_value].copy()

    # Ensure numeric for x and y; drop rows with NaNs afterwards
    df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
    df[x_col2] = pd.to_numeric(df[x_col2], errors="coerce")
    df[y_col] = pd.to_numeric(df[y_col], errors="coerce")
    df = df.dropna(subset=[x_col, x_col2, y_col])

    # Preserve existing order; find first strictly-decreasing run in x_col
    x = (df[x_col] - df[x_col2]).to_numpy()
    start_idx = None
    if len(x) >= decreasing_run:
        # scan windows of size `decreasing_run`
        for s in range(0, len(x) - decreasing_run + 1):
            # strictly decreasing over the window?
            if all(x[s + k] > x[s + k + 1] for k in range(decreasing_run - 1)):
                start_idx = s
                break

    # Apply cutoff if a run was found
    if start_idx is not None:
        last_keep = max(0, min(len(df) - 1, start_idx + cutoff_delta))
        df = df.iloc[: last_keep + 1]  # inclusive

    # Prepare axes
    created_fig = False
    if ax is None:
        fig, ax = plt.subplots()
        created_fig = True

    # Plot
    ax.plot((df[x_col] - df[x_col2]).to_numpy(), df[y_col].to_numpy(), **plot_kwargs)
    ax.set_xlabel(x_col)
    ax.set_ylabel(y_col)
    ax.set_ylim((0, 5010))
    ax.set_title(f"{name_value}: {y_col} vs {x_col}")

    # Optionally tighten layout if we created the figure
    if created_fig:
        try:
            fig.tight_layout()
        except Exception:
            pass

    return ax, df


def plot_requests_vs_success_rate(
    csv_path: str,
    x_col: str = "Requests/s",
    x_col2: str = "Failures/s",
    name_col: str = "Name",
    name_value: str = "Aggregated",
    decreasing_run: int = 5,  # consecutive strictly-decreasing points to trigger cutoff
    cutoff_delta: int = 0,  # keep rows up to (start_index_of_run + cutoff_delta), inclusive
    ax: Optional[
        plt.Axes
    ] = None,  # pass an existing axes to draw on, or leave None to create one
    **plot_kwargs,
) -> Tuple[plt.Axes, pd.DataFrame]:
    # Read & filter
    df = pd.read_csv(csv_path)
    df = df[df[name_col] == name_value].copy()
    y_col = "success_rate"

    # Ensure numeric for x and y; drop rows with NaNs afterwards
    df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
    df[x_col2] = pd.to_numeric(df[x_col2], errors="coerce")
    df[y_col] = pd.to_numeric(
        ((df[x_col] - df[x_col2]) / df[x_col]) * 100, errors="coerce"
    )
    df = df.dropna(subset=[x_col, x_col2, y_col])

    # Preserve existing order; find first strictly-decreasing run in x_col
    x = (df[x_col] - df[x_col2]).to_numpy()
    start_idx = None
    if len(x) >= decreasing_run:
        # scan windows of size `decreasing_run`
        for s in range(0, len(x) - decreasing_run + 1):
            # strictly decreasing over the window?
            if all(x[s + k] > x[s + k + 1] for k in range(decreasing_run - 1)):
                start_idx = s
                break

    # Apply cutoff if a run was found
    if start_idx is not None:
        last_keep = max(0, min(len(df) - 1, start_idx + cutoff_delta))
        df = df.iloc[: last_keep + 1]  # inclusive

    ax.plot((df[x_col] - df[x_col2]).to_numpy(), df[y_col].to_numpy(), **plot_kwargs)
    ax.set_title(f"{name_value}: {y_col} vs {x_col}")

    return ax, df


def plot_best(
    data: pd.DataFrame,
    samples: list[int],
    axes: list[plt.Axes],
    results_dir: pathlib.Path,
    label: str,
):
    csv_max = None
    max_rps = 0

    for idx, row in data.iterrows():
        next_csv, next_rps = _get_best_sample_by_rps(row.task, samples, results_dir)
        if next_rps > max_rps:
            max_rps = next_rps
            csv_max = next_csv

    if csv_max is not None:
        plot_requests_vs_percentile(csv_max, ax=axes[0], label=label)
        plot_requests_vs_success_rate(csv_max, ax=axes[1], label=label)

    axes[0].set_xlabel("Achieved RPS")
    axes[0].set_ylabel("P99 [ms]")
    axes[0].set_title("99th Percentile Latency vs RPS")

    axes[1].set_xlabel("Achieved RPS")
    axes[1].set_ylabel("Success Rate [%]")
    axes[1].set_title("Success Rate vs RPS")


def compare_frameworks_and_models(
    data: pd.DataFrame,
    results_dir: pathlib.Path,
    samples: list[int],
    output_dir: pathlib.Path | None = None,
):
    nb_plots = len(data.scenario.unique())
    if nb_plots == 0:
        return
    nb_rows = (nb_plots + 1) // 2

    fig, axes = plt.subplots(nb_rows, 2, figsize=(18, 5 * nb_rows))
    if nb_rows == 1:
        axes = axes.reshape(1, -1)
    ax_i = 0

    for (scenario,), data_s in data.groupby(["scenario"]):
        ax = axes[ax_i // 2][ax_i % 2]
        ax.set_title(scenario)

        data_best = pd.DataFrame(columns=data.model.unique())

        for idx, row in data_s.iterrows():
            _, max_rps = _get_best_sample_by_rps(row.task, samples, results_dir)
            data_best.loc[row.framework, row.model] = max_rps

        data_best.plot(kind="bar", ax=ax, stacked=False)
        ax.set_ylabel("Max Requests/s")
        ax.tick_params(axis="x", labelrotation=45)
        ax_i += 1

    fig.tight_layout()
    out_dir = output_dir or (results_dir / "performance")
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "framework_performance_comparison.png")


def error_rate_vs_rps_over_time(
    data: pd.DataFrame,
    results_dir: pathlib.Path,
    samples: list[int],
    output_dir: pathlib.Path | None = None,
):
    nb_plots = len(data.scenario.unique())
    if nb_plots == 0:
        return
    nb_rows = (nb_plots + 1) // 2

    fig, axes = plt.subplots(nb_rows, 2, figsize=(18, 5 * nb_rows))
    if nb_rows == 1:
        axes = axes.reshape(1, -1)
    ax_i = 0

    cmap = colormaps["gist_rainbow"]
    my_cmap = cm.colors.ListedColormap(cmap(np.linspace(0, 0.4, 256)))
    norm = Normalize(vmin=0.8, vmax=1.0)
    ls = ["-", "--", ":", "-."]
    lc = None

    for (scenario,), data_s in data.groupby(["scenario"]):
        ax = axes[ax_i // 2][ax_i % 2]
        ax.set_title(scenario)

        data_best = pd.DataFrame(columns=["csv", "rps"])

        for (model,), rows in data_s.groupby(["model"]):
            csv, rps, framework = _get_best_framework_by_rps(rows, samples, results_dir)
            data_best.loc[f"{model}-{framework}", "csv"] = csv
            data_best.loc[f"{model}-{framework}", "rps"] = rps

        ls_i = 0
        lines = []
        legends = []
        for idx, row in data_best.iterrows():
            if row.csv is None:
                continue
            df = pd.read_csv(row.csv)
            df["Timestamp"] -= df["Timestamp"].min()
            df["rps"] = df["Requests/s"] - df["Failures/s"]
            df["rps_avg"] = df["rps"].rolling(window=10).max().rolling(window=10).mean()
            df["success_rate"] = (df["Requests/s"] - df["Failures/s"]) / df[
                "Requests/s"
            ]

            points = np.array([df["Timestamp"], df["rps_avg"]]).T.reshape(-1, 1, 2)
            segments = np.concatenate([points[:-1], points[1:]], axis=1)

            lc = LineCollection(
                segments, cmap=my_cmap, norm=norm, linestyles=ls[ls_i % len(ls)]
            )
            lc.set_array(df["success_rate"])
            ax.add_collection(lc)
            ax.set_xlim(0, df["Timestamp"].max())
            ax.set_ylim(0, df["rps_avg"].max())
            ls_i += 1
            lines.append(lc)
            legends.append(idx)

        if lines:
            ax.legend(lines, legends, loc="best")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Requests/s")
        ax_i += 1

    if lc:
        cbar = fig.colorbar(lc)
        cbar.set_label("Success Rate")
    fig.tight_layout()
    out_dir = output_dir or (results_dir / "performance")
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "model_perf_comparison.png", dpi=600)


def detailed_single_app_performance(
    data: pd.DataFrame,
    results_dir: pathlib.Path,
    samples: list[int],
    output_dir: pathlib.Path | None = None,
):
    for (scenario,), scenario_data in data.groupby(["scenario"]):
        # Example data
        x = np.linspace(0, 10, 100)

        # Grid size
        rows = scenario_data["framework"].unique()
        cols = scenario_data["model"].unique()
        sp = scenario_data.task.iloc[0].safety_prompt

        fig, axes = plt.subplots(
            len(rows),
            len(cols),
            figsize=(15, 14),
            sharex=True,
            sharey=True,
            squeeze=False,
        )
        fig.suptitle(
            f"Performance metrics - '{scenario}' (safety_prompt: {sp})",
            fontsize=14,
            weight="bold",
        )

        # Add column titles
        for ax, col_title in zip(axes[0], cols):
            ax.set_title(col_title, fontsize=11, pad=12)

        # Add row titles
        for ax, row_title in zip(axes[:, 0], rows):
            ax.set_ylabel(f"*{row_title}*\nRequests/s", fontsize=11, labelpad=12)

        cmap = colormaps["Set1"]
        colors = [cmap(i) for i in range(7)]
        y_lim = 0

        # Fill each subplot with sample data
        max_ts = 0
        for (framework,), fw_data in scenario_data.groupby(["framework"]):
            for idx, row in fw_data.iterrows():
                csv, rps = _get_best_sample_per_task(row.task, samples, results_dir)
                if csv is not None:
                    df = pd.read_csv(csv)
                    i = np.where(rows == framework)[0][0]
                    j = np.where(cols == row.model)[0][0]
                    lines = []
                    if df["Requests/s"].max() > y_lim:
                        y_lim = df["Requests/s"].max()

                    df = df[df["Name"] == "Aggregated"]

                    df["Timestamp"] -= df["Timestamp"].min()
                    max_ts = max(max_ts, df["Timestamp"].max())
                    df["Throughput"] = df["Requests/s"] - df["Failures/s"]
                    lines.append(
                        axes[i, j].plot(
                            df["Timestamp"],
                            df["User Count"],
                            label="User count",
                            color=colors[0],
                        )[0]
                    )
                    # apply 2s negative offset - due to how locust computes R/s and F/s
                    lines.append(
                        axes[i, j].plot(
                            df["Timestamp"] - 2,
                            df["Throughput"],
                            label="Successful req/s",
                            color=colors[1],
                        )[0]
                    )
                    lines.append(
                        axes[i, j].plot(
                            df["Timestamp"] - 2,
                            df["Requests/s"],
                            label="Served req/s",
                            color=colors[2],
                        )[0]
                    )

                    y_2 = axes[i, j].twinx()
                    y_2.set_ylim(0, 100)
                    perf = _get_performance(csv)
                    if perf is not None:
                        lines.append(
                            y_2.plot(
                                perf["Timestamp"],
                                perf["cpu_usage"]
                                .rolling(window=5, center=True, min_periods=1)
                                .mean(),
                                label="CPU usage (%)",
                                color=colors[3],
                            )[0]
                        )
                        lines.append(
                            y_2.plot(
                                perf["Timestamp"],
                                perf["mem_usage"]
                                .rolling(window=5, center=True, min_periods=1)
                                .mean(),
                                label="Memory usage (%)",
                                color=colors[4],
                            )[0]
                        )
                        lines.append(
                            y_2.plot(
                                perf["Timestamp"],
                                perf["network_rx_usage"]
                                .rolling(window=5, center=True, min_periods=1)
                                .mean(),
                                label="Network Rx (MB/s)",
                                color=colors[5],
                            )[0]
                        )
                        lines.append(
                            y_2.plot(
                                perf["Timestamp"],
                                perf["network_tx_usage"]
                                .rolling(window=5, center=True, min_periods=1)
                                .mean(),
                                label="Network Tx (MB/s)",
                                color=colors[6],
                            )[0]
                        )

                    labels = [line.get_label() for line in lines]
                    axes[i, j].legend(lines, labels, loc="upper left")

        top_y = y_lim * 1.1 if y_lim > 0 else 1.0
        for i in range(len(rows)):
            for j in range(len(cols)):
                axes[i, j].set_xlim(0, max_ts if max_ts > 0 else 180)
                axes[i, j].set_ylim(0, top_y)

        for i in range(len(cols)):
            axes[-1, i].set_xlabel("Time (s)")
        for i in range(len(rows)):
            ax = axes[i, -1].twinx()
            ax.set_ylim(0, 100)
            ax.set_ylabel("Usage (%)\nNetwork speed (MB/s)")

        plt.tight_layout()
        out_dir = output_dir or (results_dir / "performance")
        out_dir.mkdir(parents=True, exist_ok=True)
        plt.savefig(out_dir / f"detailed_performance_{scenario}_{sp}.png", dpi=300)


def _get_performance(csv: str):
    perf_csv = os.path.join(os.path.dirname(csv), "server_performance.csv")
    if not os.path.exists(perf_csv):
        logging.warning(f"Path does not exist: {perf_csv}")
        return None

    perf = pd.read_csv(perf_csv)
    perf["cpu_usage"] *= 100
    perf["mem_usage"] = (
        perf["mem_used_mbytes"]
        / (perf["mem_used_mbytes"] + perf["mem_free_mbytes"])
        * 100
    )
    perf["Timestamp"] = pd.to_datetime(perf["timestamp"]).astype("int64") // 10**9
    perf["Timestamp"] -= perf["Timestamp"].min()
    perf["network_rx_usage"] = perf["network_rx_bytes"] / 2**20
    perf["network_tx_usage"] = perf["network_tx_bytes"] / 2**20

    return perf


def _get_best_sample_per_task(task, samples: list[int], results_dir: pathlib.Path):
    max_rps = 0
    csv_max = None

    for sample in samples:
        csv_path = task.get_bench_results_csv_path(
            results_dir, sample, task.scenario.performance_tests[0]
        )
        if not csv_path.exists():
            logging.warning(f"Path does not exist: {csv_path}")
            continue
        df = pd.read_csv(csv_path)

        if max(df["Requests/s"] - df["Failures/s"]) > max_rps:
            max_rps = max(df["Requests/s"] - df["Failures/s"])
            csv_max = csv_path

    return csv_max, max_rps


def _get_best_sample_by_rps(task, samples: list[int], results_dir: pathlib.Path):
    max_rps = 0
    csv_max = None
    # find the best sample for each model - framework combination
    for sample in samples:
        for test in task.scenario.performance_tests:
            csv_path = task.get_bench_results_csv_path(results_dir, sample, test)
            if not csv_path.exists():
                logging.warning(f"Path does not exist: {csv_path}")
                continue
            df = pd.read_csv(csv_path)

            if max(df["Requests/s"] - df["Failures/s"]) > max_rps:
                max_rps = max(df["Requests/s"] - df["Failures/s"])
                csv_max = csv_path

    return csv_max, max_rps


def _get_best_framework_by_rps(
    tasks: pd.DataFrame, samples: list[int], results_dir: pathlib.Path
):
    max_rps = 0
    csv_max = None
    framework_max = None
    # find the best sample for framework
    for idx, row in tasks.iterrows():
        next_csv, next_rps = _get_best_sample_by_rps(row.task, samples, results_dir)
        if next_rps > max_rps:
            max_rps = next_rps
            csv_max = next_csv
            framework_max = row.framework

    return csv_max, max_rps, framework_max


def _prepare_db_timeseries(db_csv_path: str) -> pd.DataFrame:
    db = pd.read_csv(db_csv_path)
    if db.empty:
        return db
    db["ts"] = pd.to_numeric(db["ts"], errors="coerce")
    db["stmt_calls"] = pd.to_numeric(db["stmt_calls"], errors="coerce")
    db["stmt_total_exec_time_ms"] = pd.to_numeric(
        db["stmt_total_exec_time_ms"], errors="coerce"
    )
    db = db.dropna(subset=["ts", "stmt_calls", "stmt_total_exec_time_ms"])
    if db.empty:
        return db
    db = db.sort_values("ts").reset_index(drop=True)
    db["t"] = db["ts"] - db["ts"].min()
    db["d_calls"] = db["stmt_calls"].diff()
    db["d_exec_ms"] = db["stmt_total_exec_time_ms"].diff()

    # Average DB statement execution time over each interval.
    # Guard against division by zero or counter resets.
    db["avg_db_stmt_ms"] = np.where(
        db["d_calls"] > 0,
        db["d_exec_ms"] / db["d_calls"],
        np.nan,
    )
    db.loc[
        (db["avg_db_stmt_ms"] < 0) | np.isinf(db["avg_db_stmt_ms"]), "avg_db_stmt_ms"
    ] = np.nan
    return db


def _prepare_locust_timeseries(stats_csv_path: str, rolling_window: int) -> pd.DataFrame:
    loc = pd.read_csv(stats_csv_path)
    loc = loc[loc["Name"] == "Aggregated"].copy()
    if loc.empty:
        return loc

    loc["Timestamp"] = pd.to_numeric(loc["Timestamp"], errors="coerce")
    loc["Requests/s"] = pd.to_numeric(loc["Requests/s"], errors="coerce")
    loc["Failures/s"] = pd.to_numeric(loc["Failures/s"], errors="coerce")
    loc["User Count"] = pd.to_numeric(loc.get("User Count"), errors="coerce")
    loc["95%"] = pd.to_numeric(loc["95%"], errors="coerce")
    loc["99%"] = pd.to_numeric(loc["99%"], errors="coerce")
    loc["Total Average Response Time"] = pd.to_numeric(
        loc["Total Average Response Time"], errors="coerce"
    )
    loc = loc.dropna(
        subset=[
            "Timestamp",
            "Requests/s",
            "Failures/s",
            "95%",
            "99%",
            "Total Average Response Time",
        ]
    )
    if loc.empty:
        return loc

    loc["achieved_rps"] = loc["Requests/s"] - loc["Failures/s"]
    loc["served_rps"] = loc["Requests/s"]
    loc = loc[loc["User Count"] > 0].copy()
    if loc.empty:
        return loc

    rw = max(1, int(rolling_window))
    loc = loc.sort_values("Timestamp")
    loc["Timestamp"] = loc["Timestamp"].astype(float)
    loc["backend_mean_ms"] = (
        loc["Total Average Response Time"].rolling(window=rw, min_periods=1).mean()
    )
    loc["backend_p95_ms"] = loc["95%"].rolling(window=rw, min_periods=1).mean()
    loc["backend_p99_ms"] = loc["99%"].rolling(window=rw, min_periods=1).mean()

    return loc


def _prepare_backend_db_merged(
    *,
    stats_csv_path: str,
    db_csv_path: str,
    rolling_window: int,
) -> pd.DataFrame:
    """
    Common core used by both:
      - plot_backend_vs_db_latency_by_rps (across many runs)
      - plot_backend_vs_db_latency_for_run_dir (single run)
    """
    loc = _prepare_locust_timeseries(stats_csv_path, rolling_window=rolling_window)
    if loc.empty:
        return loc

    db = _prepare_db_timeseries(db_csv_path)
    if db.empty:
        return db

    rw = max(1, int(rolling_window))
    db = db.sort_values("ts")
    db["ts"] = db["ts"].astype(float)
    db["db_mean_ms"] = db["avg_db_stmt_ms"].rolling(window=rw, min_periods=1).mean()

    merged = pd.merge_asof(
        loc[
            [
                "Timestamp",
                "achieved_rps",
                "served_rps",
                "User Count",
                "backend_mean_ms",
                "backend_p95_ms",
                "backend_p99_ms",
            ]
        ],
        db[["ts", "db_mean_ms"]],
        left_on="Timestamp",
        right_on="ts",
        direction="nearest",
    ).dropna(
        subset=[
            "User Count",
            "backend_mean_ms",
            "backend_p95_ms",
            "backend_p99_ms",
            "db_mean_ms",
        ]
    )
    return merged


def plot_backend_vs_db_latency_by_rps(
    task,
    samples: list[int],
    results_dir: pathlib.Path,
    out_path: str,
    rolling_window: int = 3,
    n_bins: int = 35,
) -> bool:
    """
    Aggregate selected samples and plot latency-vs-RPS comparison:
      - Backend mean / p95 / p99 (blue shades)
      - DB mean / p95 / p99 (red shades)
      - RPS observation density (green, secondary axis)

    Returns True if a plot was generated, False if insufficient data.
    """
    rows: list[pd.DataFrame] = []
    rw = max(1, int(rolling_window))

    for sample in samples:
        stats_path = task.get_bench_results_csv_path(
            results_dir, sample, task.scenario.performance_tests[0]
        )
        # stats_path is .../locust/results/<test>_stats_history.csv;
        # db_performance.csv lives under diagnostics/distributed/.
        db_path = (
            stats_path.parent.parent.parent
            / "diagnostics"
            / "distributed"
            / "database"
            / "db_performance.csv"
        )
        if not stats_path.exists() or not db_path.exists():
            continue
        merged = _prepare_backend_db_merged(
            stats_csv_path=str(stats_path),
            db_csv_path=str(db_path),
            rolling_window=rw,
        )
        if not merged.empty:
            rows.append(merged)

    if not rows:
        # Most common cause: the run had 100% failures, so achieved_rps == 0 for all rows.
        # Create a small placeholder image so it's obvious why the plot is missing.
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.axis("off")
        ax.text(
            0.5,
            0.5,
            "No successful requests to plot.\n"
            "This plot requires achieved_rps > 0 (Requests/s - Failures/s).\n"
            "If you saw many HTTP 502s, fix the load balancer / upstream first, then re-run.",
            ha="center",
            va="center",
            fontsize=12,
        )
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        fig.tight_layout()
        fig.savefig(out_path, dpi=200, bbox_inches="tight")
        plt.close(fig)
        return True

    all_rows = pd.concat(rows, ignore_index=True)
    rps_min = float(all_rows["User Count"].min())
    rps_max = float(all_rows["User Count"].max())
    if rps_max <= rps_min:
        return False

    bin_edges = np.linspace(rps_min, rps_max, max(6, n_bins))
    all_rows["rps_bin"] = pd.cut(
        all_rows["User Count"], bins=bin_edges, include_lowest=True
    )
    grouped = all_rows.groupby("rps_bin", observed=True)

    bmean = grouped["backend_mean_ms"].mean()
    bp95 = grouped["backend_p95_ms"].mean()
    bp99 = grouped["backend_p99_ms"].mean()

    dmean = grouped["db_mean_ms"].mean()
    dp95 = grouped["db_mean_ms"].quantile(0.95)
    dp99 = grouped["db_mean_ms"].quantile(0.99)
    counts = grouped["User Count"].count()

    mids = np.array(
        [
            (idx.left + idx.right) / 2
            for idx in bmean.index
            if pd.notna(idx.left) and pd.notna(idx.right)
        ]
    )
    if len(mids) == 0:
        return False

    # Align all series to valid bins that produced midpoints
    valid_idx = bmean.index[: len(mids)]
    bmean = bmean.loc[valid_idx].to_numpy()
    bp95 = bp95.loc[valid_idx].to_numpy()
    bp99 = bp99.loc[valid_idx].to_numpy()
    dmean = dmean.loc[valid_idx].to_numpy()
    dp95 = dp95.loc[valid_idx].to_numpy()
    dp99 = dp99.loc[valid_idx].to_numpy()
    counts = counts.loc[valid_idx].to_numpy()

    fig, ax = plt.subplots(figsize=(12, 7))

    # Backend in blue shades (left axis)
    ax.plot(mids, bmean, color="#1f77b4", linewidth=2.2, label="Backend mean")
    ax.plot(mids, bp95, color="#4f9ddf", linewidth=2.0, label="Backend p95")
    ax.plot(mids, bp99, color="#9ac4ee", linewidth=2.0, label="Backend p99")

    # DB in red shades (right axis)
    ax_db = ax.twinx()
    ax_db.plot(mids, dmean, color="#d62728", linewidth=2.2, label="DB mean")
    ax_db.plot(mids, dp95, color="#ef6a6a", linewidth=2.0, label="DB p95")
    ax_db.plot(mids, dp99, color="#f6aaaa", linewidth=2.0, label="DB p99")

    ax.set_xlabel("Attempted Load (User Count)")
    ax.set_ylabel("Backend latency (ms)")
    ax_db.set_ylabel("DB latency (ms)")
    ax.set_title(
        f"Latency distribution by Attempted Load: backend vs database\n{task.model} | {task.scenario.id} | {task.env.id}"
    )
    ax.grid(alpha=0.25, linestyle="--")

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax_db.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return True


def plot_backend_vs_db_latency_for_run_dir(
    run_dir: pathlib.Path,
    out_dir: pathlib.Path | None = None,
    rolling_window: int = 3,
    n_bins: int = 35,
) -> pathlib.Path:
    """
    Plot backend vs DB latency for a single per-run directory.

    Expected files inside run_dir:
      - locust/results/*_stats_history.csv (Locust stats history)
      - diagnostics/distributed/hosts/*/db_performance.csv (distributed bench) or
        diagnostics/distributed/database/db_performance.csv (local docker bench)
    Optional:
      - server_performance.csv (not required for this plot)
    """
    run_dir = pathlib.Path(run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist or is not a directory: {run_dir}")

    stats_candidates = sorted((run_dir / "locust" / "results").glob("*_stats_history.csv"))
    if not stats_candidates:
        raise FileNotFoundError(
            f"No locust stats_history CSV found in {run_dir} (expected locust/results/*_stats_history.csv)"
        )
    stats_path = stats_candidates[0]

    db_paths = sorted(
        (run_dir / "diagnostics" / "distributed" / "hosts").glob("*/db_performance.csv")
    )
    if not db_paths:
        local_db = run_dir / "diagnostics" / "distributed" / "database" / "db_performance.csv"
        db_paths = [local_db] if local_db.exists() else []
    if not db_paths:
        raise FileNotFoundError(
            f"Missing {run_dir}/diagnostics/distributed/{{hosts/*,database}}/db_performance.csv"
        )
    db_path = db_paths[0]

    rw = max(1, int(rolling_window))
    merged = _prepare_backend_db_merged(
        stats_csv_path=str(stats_path),
        db_csv_path=str(db_path),
        rolling_window=rw,
    )
    if merged.empty:
        raise ValueError(f"Failed to align locust stats with db metrics for {run_dir}")

    rps_min = float(merged["User Count"].min())
    rps_max = float(merged["User Count"].max())
    if rps_max <= rps_min:
        raise ValueError(f"Not enough variation in User Count to plot for {run_dir}")

    bin_edges = np.linspace(rps_min, rps_max, max(6, int(n_bins)))
    merged["rps_bin"] = pd.cut(merged["User Count"], bins=bin_edges, include_lowest=True)
    grouped = merged.groupby("rps_bin", observed=True)

    bmean = grouped["backend_mean_ms"].mean()
    bp95 = grouped["backend_p95_ms"].mean()
    bp99 = grouped["backend_p99_ms"].mean()

    dmean = grouped["db_mean_ms"].mean()
    dp95 = grouped["db_mean_ms"].quantile(0.95)
    dp99 = grouped["db_mean_ms"].quantile(0.99)
    counts = grouped["User Count"].count()

    mids = np.array(
        [
            (idx.left + idx.right) / 2
            for idx in bmean.index
            if pd.notna(idx.left) and pd.notna(idx.right)
        ]
    )
    if len(mids) == 0:
        raise ValueError(f"No bins produced midpoints for {run_dir}")

    valid_idx = bmean.index[: len(mids)]
    bmean = bmean.loc[valid_idx].to_numpy()
    bp95 = bp95.loc[valid_idx].to_numpy()
    bp99 = bp99.loc[valid_idx].to_numpy()
    dmean = dmean.loc[valid_idx].to_numpy()
    dp95 = dp95.loc[valid_idx].to_numpy()
    dp99 = dp99.loc[valid_idx].to_numpy()
    counts = counts.loc[valid_idx].to_numpy()

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.plot(mids, bmean, color="#1f77b4", linewidth=2.2, label="Backend mean")
    ax.plot(mids, bp95, color="#4f9ddf", linewidth=2.0, label="Backend p95")
    ax.plot(mids, bp99, color="#9ac4ee", linewidth=2.0, label="Backend p99")

    ax_db = ax.twinx()
    ax_db.plot(mids, dmean, color="#d62728", linewidth=2.2, label="DB mean")
    ax_db.plot(mids, dp95, color="#ef6a6a", linewidth=2.0, label="DB p95")
    ax_db.plot(mids, dp99, color="#f6aaaa", linewidth=2.0, label="DB p99")

    ax.set_xlabel("Attempted Load (User Count)")
    ax.set_ylabel("Backend latency (ms)")
    ax_db.set_ylabel("DB latency (ms)")
    ax.set_title(f"Backend vs DB latency by Attempted Load\n{run_dir}")
    ax.grid(alpha=0.25, linestyle="--")

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax_db.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    out_dir = out_dir or (run_dir / "plots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "backend_vs_db_latency_by_rps.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_throughput_over_time_for_run_dir(
    run_dir: pathlib.Path,
    out_dir: pathlib.Path | None = None,
    rolling_window: int = 5,
) -> pathlib.Path:
    """
    Plot served vs successful throughput over time for a single per-run directory.

    Uses the first locust/results/*_stats_history.csv found in run_dir.
    """
    run_dir = pathlib.Path(run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist or is not a directory: {run_dir}")

    stats_candidates = sorted((run_dir / "locust" / "results").glob("*_stats_history.csv"))
    if not stats_candidates:
        raise FileNotFoundError(
            f"No locust stats_history CSV found in {run_dir} (expected locust/results/*_stats_history.csv)"
        )
    stats_path = stats_candidates[0]

    df = pd.read_csv(stats_path)
    df = df[df["Name"] == "Aggregated"].copy()
    if df.empty:
        raise ValueError(f"No Aggregated rows in {stats_path}")

    df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce")
    df["User Count"] = pd.to_numeric(df.get("User Count"), errors="coerce")
    df["Requests/s"] = pd.to_numeric(df["Requests/s"], errors="coerce")
    df["Failures/s"] = pd.to_numeric(df["Failures/s"], errors="coerce")
    df = df.dropna(subset=["Timestamp", "Requests/s", "Failures/s"])
    if df.empty:
        raise ValueError(f"Stats history in {stats_path} is empty after numeric coercion")

    # Locust timestamps are already relative seconds in stats_history; normalize anyway.
    df = df.sort_values("Timestamp").reset_index(drop=True)
    df["t"] = df["Timestamp"] - df["Timestamp"].min()
    df["successful_rps"] = df["Requests/s"] - df["Failures/s"]
    df["served_rps"] = df["Requests/s"]
    df["failures_rps"] = df["Failures/s"]

    rw = max(1, int(rolling_window))
    df["successful_rps_smooth"] = df["successful_rps"].rolling(window=rw, min_periods=1).mean()
    df["served_rps_smooth"] = df["served_rps"].rolling(window=rw, min_periods=1).mean()
    df["failures_rps_smooth"] = df["failures_rps"].rolling(window=rw, min_periods=1).mean()

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(df["t"], df["served_rps_smooth"], color="#ff7f0e", linewidth=2.0, label="Served req/s")
    ax.plot(df["t"], df["successful_rps_smooth"], color="#2ca02c", linewidth=2.2, label="Successful req/s")
    ax.plot(df["t"], df["failures_rps_smooth"], color="#d62728", linewidth=1.8, linestyle="--", label="Failures/s")

    ax.set_xlabel("Time (s)")
    ax.set_title(f"Throughput over time\n{run_dir}")
    ax.grid(alpha=0.25, linestyle="--")
    if "User Count" in df.columns and df["User Count"].notna().any():
        ax.plot(
            df["t"],
            df["User Count"].rolling(window=rw, min_periods=1).mean(),
            color="#1f77b4",
            linewidth=1.8,
            alpha=0.7,
            label="User count",
        )
        ax.set_ylabel("Requests/s & Users")
        ax.legend(loc="upper left")
    else:
        ax.set_ylabel("Requests/s")
        ax.legend(loc="upper left")

    out_dir = out_dir or (run_dir / "plots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "throughput_over_time.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_throughput_by_rps_for_run_dir(
    run_dir: pathlib.Path,
    out_dir: pathlib.Path | None = None,
    *,
    rps_bucket_width: float = 25.0,
    rps_bucket_min: float | None = None,
    rps_bucket_max: float | None = None,
    rolling_window: int = 1,
) -> pathlib.Path:
    """
    Plot throughput aggregated into buckets by served RPS (not by time).

    X-axis: served RPS bucket midpoints.
    Y-axis: per-bucket average of:
      - Successful req/s (Requests/s - Failures/s)
      - Served req/s (Requests/s)
      - Failures/s (Failures/s)

    Notes:
    - Locust reports Requests/s and Failures/s at ~1s cadence; optional rolling_window
      smooths those series before bucketing (default: no smoothing).
    - Bucketing uses served RPS to match what the system attempted to serve; successful
      throughput is shown as the averaged achieved throughput within each served bucket.
    """
    run_dir = pathlib.Path(run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist or is not a directory: {run_dir}")

    stats_candidates = sorted((run_dir / "locust" / "results").glob("*_stats_history.csv"))
    if not stats_candidates:
        raise FileNotFoundError(
            f"No locust stats_history CSV found in {run_dir} (expected locust/results/*_stats_history.csv)"
        )
    stats_path = stats_candidates[0]

    df = pd.read_csv(stats_path)
    df = df[df["Name"] == "Aggregated"].copy()
    if df.empty:
        raise ValueError(f"No Aggregated rows in {stats_path}")

    df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce")
    df["Requests/s"] = pd.to_numeric(df["Requests/s"], errors="coerce")
    df["Failures/s"] = pd.to_numeric(df["Failures/s"], errors="coerce")
    df["User Count"] = pd.to_numeric(df.get("User Count"), errors="coerce")
    df = df.dropna(subset=["Timestamp", "Requests/s", "Failures/s", "User Count"])
    if df.empty:
        raise ValueError(f"Stats history in {stats_path} is empty after numeric coercion")

    df = df.sort_values("Timestamp").reset_index(drop=True)
    df["served_rps"] = df["Requests/s"].astype(float)
    df["failures_rps"] = df["Failures/s"].astype(float)
    df["successful_rps"] = (df["Requests/s"] - df["Failures/s"]).astype(float)
    df["user_count"] = df["User Count"].astype(float)

    rw = max(1, int(rolling_window))
    df["user_count_smooth"] = df["user_count"].rolling(window=rw, min_periods=1).mean()
    df["served_rps_smooth"] = df["served_rps"].rolling(window=rw, min_periods=1).mean()
    df["failures_rps_smooth"] = (
        df["failures_rps"].rolling(window=rw, min_periods=1).mean()
    )
    df["successful_rps_smooth"] = (
        df["successful_rps"].rolling(window=rw, min_periods=1).mean()
    )

    # Choose bucket range
    rps_w = float(rps_bucket_width)
    if not np.isfinite(rps_w) or rps_w <= 0:
        raise ValueError(f"rps_bucket_width must be > 0, got {rps_bucket_width}")

    served_min = float(df["user_count_smooth"].min())
    served_max = float(df["user_count_smooth"].max())
    lo = served_min if rps_bucket_min is None else float(rps_bucket_min)
    hi = served_max if rps_bucket_max is None else float(rps_bucket_max)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        raise ValueError(
            f"Invalid bucket range lo={lo} hi={hi} (served_min={served_min} served_max={served_max})"
        )

    # Bucket by served RPS; include 0 as a natural floor if range crosses it.
    lo2 = min(lo, 0.0)
    # Make sure the last edge includes hi.
    n_steps = int(np.ceil((hi - lo2) / rps_w))
    if n_steps < 2:
        n_steps = 2
    edges = lo2 + (np.arange(n_steps + 1, dtype=float) * rps_w)
    if edges[-1] < hi:
        edges = np.append(edges, edges[-1] + rps_w)

    df["served_rps_bin"] = pd.cut(
        df["user_count_smooth"], bins=edges, include_lowest=True
    )
    grouped = df.groupby("served_rps_bin", observed=True)

    served_mean = grouped["served_rps_smooth"].mean()
    succ_mean = grouped["successful_rps_smooth"].mean()
    fail_mean = grouped["failures_rps_smooth"].mean()
    n_obs = grouped["served_rps_smooth"].count()

    # Drop empty bins (should be none with observed=True, but keep defensive)
    out = (
        pd.DataFrame(
            {
                "served_mean": served_mean,
                "successful_mean": succ_mean,
                "failures_mean": fail_mean,
                "n": n_obs,
            }
        )
        .reset_index()
        .dropna(subset=["served_rps_bin", "served_mean", "successful_mean", "failures_mean"])
    )
    if out.empty:
        raise ValueError(f"No data left after bucketing for {run_dir}")

    def _mid(iv) -> float:
        try:
            return float(iv.left + iv.right) / 2.0
        except Exception:
            return float("nan")

    out["served_bin_mid"] = out["served_rps_bin"].apply(_mid)
    out = out.dropna(subset=["served_bin_mid"]).sort_values("served_bin_mid")
    if out.empty:
        raise ValueError(f"No valid buckets produced midpoints for {run_dir}")

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(
        out["served_bin_mid"].to_numpy(),
        out["served_mean"].to_numpy(),
        color="#ff7f0e",
        linewidth=2.0,
        marker="o",
        markersize=3.5,
        label="Served req/s (avg)",
    )
    ax.plot(
        out["served_bin_mid"].to_numpy(),
        out["successful_mean"].to_numpy(),
        color="#2ca02c",
        linewidth=2.2,
        marker="o",
        markersize=3.5,
        label="Successful req/s (avg)",
    )
    ax.plot(
        out["served_bin_mid"].to_numpy(),
        out["failures_mean"].to_numpy(),
        color="#d62728",
        linewidth=1.8,
        linestyle="--",
        marker="o",
        markersize=3.0,
        alpha=0.9,
        label="Failures/s (avg)",
    )

    ax.set_xlabel("Attempted Load (User Count)")
    ax.set_ylabel("Requests/s")
    ax.set_title(
        "Throughput by Attempted Load (bucket averages)\n"
        f"{run_dir}  |  bucket={rps_w:g} rps  |  smooth_window={rw}"
    )
    ax.grid(alpha=0.25, linestyle="--")
    ax.legend(loc="best")

    out_dir = out_dir or (run_dir / "plots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "throughput_by_rps.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


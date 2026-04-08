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
    fig.savefig(results_dir / "performance" / "framework_performance_comparison.png")


def error_rate_vs_rps_over_time(
    data: pd.DataFrame,
    results_dir: pathlib.Path,
    samples: list[int],
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
    fig.savefig(results_dir / "performance" / "model_perf_comparison.png", dpi=600)


def detailed_single_app_performance(
    data: pd.DataFrame, results_dir: pathlib.Path, samples: list[int]
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
        plt.savefig(
            results_dir / "performance" / f"detailed_performance_{scenario}_{sp}.png",
            dpi=300,
        )


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
        db_path = stats_path.parent / "db_performance.csv"
        if not stats_path.exists() or not db_path.exists():
            continue

        loc = pd.read_csv(stats_path)
        loc = loc[loc["Name"] == "Aggregated"].copy()
        if loc.empty:
            continue

        loc["Timestamp"] = pd.to_numeric(loc["Timestamp"], errors="coerce")
        loc["Requests/s"] = pd.to_numeric(loc["Requests/s"], errors="coerce")
        loc["Failures/s"] = pd.to_numeric(loc["Failures/s"], errors="coerce")
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
            continue
        loc["achieved_rps"] = loc["Requests/s"] - loc["Failures/s"]
        loc = loc[loc["achieved_rps"] > 0].copy()
        if loc.empty:
            continue

        db = _prepare_db_timeseries(str(db_path))
        if db.empty:
            continue

        loc = loc.sort_values("Timestamp")
        db = db.sort_values("ts")
        loc["Timestamp"] = loc["Timestamp"].astype(float)
        db["ts"] = db["ts"].astype(float)
        loc["backend_mean_ms"] = (
            loc["Total Average Response Time"].rolling(window=rw, min_periods=1).mean()
        )
        loc["backend_p95_ms"] = loc["95%"].rolling(window=rw, min_periods=1).mean()
        loc["backend_p99_ms"] = loc["99%"].rolling(window=rw, min_periods=1).mean()
        db["db_mean_ms"] = db["avg_db_stmt_ms"].rolling(window=rw, min_periods=1).mean()

        merged = pd.merge_asof(
            loc[
                [
                    "Timestamp",
                    "achieved_rps",
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
                "achieved_rps",
                "backend_mean_ms",
                "backend_p95_ms",
                "backend_p99_ms",
                "db_mean_ms",
            ]
        )
        if merged.empty:
            continue
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
    rps_min = float(all_rows["achieved_rps"].min())
    rps_max = float(all_rows["achieved_rps"].max())
    if rps_max <= rps_min:
        return False

    bin_edges = np.linspace(rps_min, rps_max, max(6, n_bins))
    all_rows["rps_bin"] = pd.cut(
        all_rows["achieved_rps"], bins=bin_edges, include_lowest=True
    )
    grouped = all_rows.groupby("rps_bin", observed=True)

    bmean = grouped["backend_mean_ms"].mean()
    bp95 = grouped["backend_p95_ms"].mean()
    bp99 = grouped["backend_p99_ms"].mean()

    dmean = grouped["db_mean_ms"].mean()
    dp95 = grouped["db_mean_ms"].quantile(0.95)
    dp99 = grouped["db_mean_ms"].quantile(0.99)
    counts = grouped["achieved_rps"].count()

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

    ax.set_xlabel("Achieved RPS")
    ax.set_ylabel("Backend latency (ms)")
    ax_db.set_ylabel("DB latency (ms)")
    ax.set_title(
        f"Latency distribution by RPS: backend vs database\n{task.model} | {task.scenario.id} | {task.env.id}"
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
      - bench_results_*_stats_history.csv (Locust stats history)
      - db_performance.csv
    Optional:
      - server_performance.csv (not required for this plot)
    """
    run_dir = pathlib.Path(run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist or is not a directory: {run_dir}")

    stats_candidates = sorted(run_dir.glob("bench_results_*_stats_history.csv"))
    if not stats_candidates:
        raise FileNotFoundError(
            f"No locust stats_history CSV found in {run_dir} (expected bench_results_*_stats_history.csv)"
        )
    stats_path = stats_candidates[0]

    db_path = run_dir / "db_performance.csv"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing db_performance.csv in {run_dir}")

    db = _prepare_db_timeseries(str(db_path))
    if db.empty:
        raise ValueError(f"db_performance.csv in {run_dir} is empty or unparsable")

    loc = pd.read_csv(stats_path)
    loc = loc[loc["Name"] == "Aggregated"].copy()
    if loc.empty:
        raise ValueError(f"No Aggregated rows in {stats_path}")

    loc["Timestamp"] = pd.to_numeric(loc["Timestamp"], errors="coerce")
    loc["Requests/s"] = pd.to_numeric(loc["Requests/s"], errors="coerce")
    loc["Failures/s"] = pd.to_numeric(loc["Failures/s"], errors="coerce")
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
        raise ValueError(f"Stats history in {stats_path} is empty after numeric coercion")

    loc["achieved_rps"] = loc["Requests/s"] - loc["Failures/s"]
    loc = loc[loc["achieved_rps"] > 0].copy()
    if loc.empty:
        raise ValueError(
            f"No successful requests to plot (achieved_rps <= 0) in {stats_path}"
        )

    rw = max(1, int(rolling_window))
    loc = loc.sort_values("Timestamp")
    db = db.sort_values("ts")
    loc["Timestamp"] = loc["Timestamp"].astype(float)
    db["ts"] = db["ts"].astype(float)
    loc["backend_mean_ms"] = (
        loc["Total Average Response Time"].rolling(window=rw, min_periods=1).mean()
    )
    loc["backend_p95_ms"] = loc["95%"].rolling(window=rw, min_periods=1).mean()
    loc["backend_p99_ms"] = loc["99%"].rolling(window=rw, min_periods=1).mean()
    db["db_mean_ms"] = db["avg_db_stmt_ms"].rolling(window=rw, min_periods=1).mean()

    merged = pd.merge_asof(
        loc[
            [
                "Timestamp",
                "achieved_rps",
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
            "achieved_rps",
            "backend_mean_ms",
            "backend_p95_ms",
            "backend_p99_ms",
            "db_mean_ms",
        ]
    )
    if merged.empty:
        raise ValueError(f"Failed to align locust stats with db metrics for {run_dir}")

    rps_min = float(merged["achieved_rps"].min())
    rps_max = float(merged["achieved_rps"].max())
    if rps_max <= rps_min:
        raise ValueError(f"Not enough RPS variation to plot for {run_dir}")

    bin_edges = np.linspace(rps_min, rps_max, max(6, int(n_bins)))
    merged["rps_bin"] = pd.cut(merged["achieved_rps"], bins=bin_edges, include_lowest=True)
    grouped = merged.groupby("rps_bin", observed=True)

    bmean = grouped["backend_mean_ms"].mean()
    bp95 = grouped["backend_p95_ms"].mean()
    bp99 = grouped["backend_p99_ms"].mean()

    dmean = grouped["db_mean_ms"].mean()
    dp95 = grouped["db_mean_ms"].quantile(0.95)
    dp99 = grouped["db_mean_ms"].quantile(0.99)
    counts = grouped["achieved_rps"].count()

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

    ax.set_xlabel("Achieved RPS")
    ax.set_ylabel("Backend latency (ms)")
    ax_db.set_ylabel("DB latency (ms)")
    ax.set_title(f"Backend vs DB latency by RPS\n{run_dir}")
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

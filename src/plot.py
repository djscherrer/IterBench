import os
import pathlib
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from typing import Optional, Tuple

import matplotlib.cm as cm
from matplotlib import colormaps
from matplotlib.colors import Normalize
from matplotlib.collections import LineCollection


def plot_requests_vs_percentile(
    csv_path: str,
    x_col: str = "Requests/s",
    x_col2: str = "Failures/s",
    y_col: str = "99%",                # any percentile column, e.g. "95%", "99.9%", etc.
    name_col: str = "Name",
    name_value: str = "Aggregated",
    decreasing_run: int = 5,           # consecutive strictly-decreasing points to trigger cutoff
    cutoff_delta: int = 0,             # keep rows up to (start_index_of_run + cutoff_delta), inclusive
    ax: Optional[plt.Axes] = None,     # pass an existing axes to draw on, or leave None to create one
    **plot_kwargs,                     # e.g. linewidth=2, marker="o"
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
    ax.set_ylim((0,5010))
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
    decreasing_run: int = 5,           # consecutive strictly-decreasing points to trigger cutoff
    cutoff_delta: int = 0,             # keep rows up to (start_index_of_run + cutoff_delta), inclusive
    ax: Optional[plt.Axes] = None,     # pass an existing axes to draw on, or leave None to create one
    **plot_kwargs
) -> Tuple[plt.Axes, pd.DataFrame]:
    # Read & filter
    df = pd.read_csv(csv_path)
    df = df[df[name_col] == name_value].copy()
    y_col = "success_rate"

    # Ensure numeric for x and y; drop rows with NaNs afterwards
    df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
    df[x_col2] = pd.to_numeric(df[x_col2], errors="coerce")
    df[y_col] = pd.to_numeric(((df[x_col] - df[x_col2]) / df[x_col]) * 100, errors="coerce")
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

def plot_best(data: pd.DataFrame, samples: list[int], axes: list[plt.Axes], results_dir: pathlib.Path, label: str):
    csv_max = None
    max_rps = 0

    axes[0].set_xlabel("Achived RPS")
    axes[0].set_ylabel("P99 [ms]")
    # axes[0].set_ylim((0, 1500))

    axes[1].set_xlabel("Achived RPS")
    axes[1].set_ylabel("Percentage")
    # axes[1].set_ylim((90, 102))

    for idx, row in data.iterrows():
        next_csv, next_rps = _get_best_sample_by_rps(row.task, samples, results_dir)
        if next_rps > max_rps:
            max_rps = next_rps
            csv_max = next_csv

    if csv_max is not None:
        plot_requests_vs_percentile(csv_max, ax=axes[0], label=label)
        plot_requests_vs_success_rate(csv_max, ax=axes[1], label=label)


def compare_frameworks_and_models(
    data: pd.DataFrame,
    results_dir: pathlib.Path,
    samples: list[int],
):
    nb_plots = len(data.scenario.unique())
    if nb_plots == 0:
        return
    nb_rows = (nb_plots+1) // 2

    fig, axes = plt.subplots(nb_rows, 2, figsize=(18, 5*nb_rows))
    if nb_rows == 1:
        axes = axes.reshape(1, -1)
    ax_i = 0

    for (scenario,), data_s in data.groupby(["scenario"]):
        ax = axes[ax_i//2][ax_i%2]
        ax.set_title(scenario)

        data_best = pd.DataFrame(columns=data.model.unique())

        for idx, row in data_s.iterrows():
            _, max_rps = _get_best_sample_by_rps(row.task, samples, results_dir)
            data_best.loc[row.framework, row.model] = max_rps

        data_best.plot(kind="bar", ax=ax, stacked=False)
        ax.tick_params(axis='x', labelrotation=45)
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

    for (scenario,), data_s in data.groupby(["scenario"]):
        ax = axes[ax_i // 2][ax_i % 2]
        ax.set_title(scenario)

        data_best = pd.DataFrame(columns=["csv", "rps"])

        for (model, ), rows in data_s.groupby(["model"]):
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
            df["success_rate"] = (df["Requests/s"] - df["Failures/s"]) / df["Requests/s"]

            points = np.array([df["Timestamp"], df["rps_avg"]]).T.reshape(-1,1,2)
            segments = np.concatenate([points[:-1], points[1:]], axis=1)

            lc = LineCollection(segments, cmap=my_cmap, norm=norm, linestyles=ls[ls_i % len(ls)])
            lc.set_array(df["success_rate"])
            ax.add_collection(lc)
            ax.set_xlim(0, df["Timestamp"].max())
            ax.set_ylim(0, df["rps_avg"].max())
            ls_i+=1
            lines.append(lc)
            legends.append(idx)

        ax.legend(lines, legends, loc='best')
        ax_i += 1

    fig.colorbar(lc)
    fig.tight_layout()
    fig.savefig(results_dir / "performance" / "model_perf_comparison.png",dpi=600)

def detailed_single_app_performance(data: pd.DataFrame, results_dir: pathlib.Path, samples: list[int]):
    for (scenario, ), scenario_data in data.groupby(["scenario"]):
        # Example data
        x = np.linspace(0, 10, 100)

        # Grid size
        rows = scenario_data["framework"].unique()
        cols = scenario_data["model"].unique()

        fig, axes = plt.subplots(len(rows), len(cols), figsize=(15, 14), sharex=True, sharey=True)
        fig.suptitle(f"Performance metrics - '{scenario}'", fontsize=14, weight="bold")

        # Add column titles
        for ax, col_title in zip(axes[0], cols):
            ax.set_title(col_title, fontsize=11, pad=12)

        # Add row titles
        for ax, row_title in zip(axes[:, 0], rows):
            ax.set_ylabel(f"*{row_title}*\nRequests/s", fontsize=11, labelpad=12)

        cmap = colormaps['Set1']
        colors = [cmap(i) for i in range(7)]
        y_lim = 0

        # todo: make target dynamic
        # Fill each subplot with sample data
        for (framework, ), fw_data in scenario_data.groupby(["framework"]):
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
                    df["Throughput"] = df["Requests/s"] - df["Failures/s"]
                    lines.append(axes[i,j].plot([i for i in range(180)], [i*120/0.65 for i in range(180)], label="Target req/s", color=colors[0])[0])
                    lines.append(axes[i,j].plot(df["Timestamp"], df["Throughput"], label="Successful req/s", color=colors[1])[0])
                    lines.append(axes[i,j].plot(df["Timestamp"], df["Requests/s"], label="Served req/s", color=colors[2])[0])

                    y_2 = axes[i,j].twinx()
                    y_2.set_ylim(0, 100)
                    perf = _get_performance(csv)
                    lines.append(y_2.plot(perf["Timestamp"], perf["cpu_usage"], label="CPU usage (%)", color=colors[3])[0])
                    lines.append(y_2.plot(perf["Timestamp"], perf["mem_usage"], label="Memory usage (%)", color=colors[4])[0])
                    lines.append(y_2.plot(perf["Timestamp"], perf["network_rx_usage"], label="Network Rx (MB/s)", color=colors[5])[0])
                    lines.append(y_2.plot(perf["Timestamp"], perf["network_tx_usage"], label="Network Tx (MB/s)",color=colors[6])[0])

                    labels = [line.get_label() for line in lines]
                    axes[i,j].legend(lines, labels, loc="upper left")

        for i in range(len(rows)):
            for j in range(len(cols)):
                axes[i,j].set_xlim(0, 180)
                axes[i,j].set_ylim(0, y_lim*1.1)

        for i in range(len(cols)):
            axes[-1, i].set_xlabel("Time (s)")
        for i in range(len(rows)):
            ax = axes[i, -1].twinx()
            ax.set_ylim(0, 100)
            ax.set_ylabel("Usage (%)\nNetwork speed (MB/s)")

        plt.tight_layout()
        plt.savefig(results_dir / "performance" / f"detailed_performance_{scenario}.png", dpi=600)


def _get_performance(csv: str):
    perf_csv = os.path.join(os.path.dirname(csv), "server_performance.csv")

    perf = pd.read_csv(perf_csv)
    perf["cpu_usage"] *= 100
    perf["mem_usage"] = perf["mem_used_mbytes"] / (perf["mem_used_mbytes"] + perf["mem_free_mbytes"]) * 100
    perf["Timestamp"] = pd.to_datetime(perf["timestamp"]).astype("int64") // 10**9
    perf["Timestamp"] -= perf["Timestamp"].min()
    perf["network_rx_usage"] = perf["network_rx_bytes"] / 2**20
    perf["network_tx_usage"] = perf["network_tx_bytes"] / 2**20

    return perf

def _get_best_sample_per_task(task, samples: list[int], results_dir: pathlib.Path):
    max_rps = 0
    csv_max = None

    for sample in samples:
        csv_path = task.get_bench_results_csv_path(results_dir, sample, task.scenario.performance_tests[0])
        if not csv_path.exists():
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
                continue
            df = pd.read_csv(csv_path)

            if max(df["Requests/s"] - df["Failures/s"]) > max_rps:
                max_rps = max(df["Requests/s"] - df["Failures/s"])
                csv_max = csv_path

    return csv_max, max_rps


def _get_best_framework_by_rps(tasks: pd.DataFrame, samples: list[int], results_dir: pathlib.Path):
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
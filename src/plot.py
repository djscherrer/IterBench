import pathlib

import pandas as pd
import matplotlib.pyplot as plt
from typing import Optional, Tuple

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
        for sample in samples:
            for test in row.task.scenario.performance_tests:
                csv_path = row.task.get_bench_results_csv_path(results_dir, sample, test)
                if not csv_path.exists():
                    continue
                df = pd.read_csv(csv_path)

                if max(df["Requests/s"]-df["Failures/s"]) > max_rps:
                    max_rps = max(df["Requests/s"]-df["Failures/s"])
                    csv_max = csv_path
    if csv_max is not None:
        plot_requests_vs_percentile(csv_max, ax=axes[0], label=label)
        plot_requests_vs_success_rate(csv_max, ax=axes[1], label=label)
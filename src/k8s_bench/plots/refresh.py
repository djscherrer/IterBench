from __future__ import annotations

import logging
from pathlib import Path

from .adaptive_ramp import regenerate_bench_plots
from .goodput_trajectory import regenerate_experiment_plots


def refresh_plots_after_bench(
    bench_dir: Path,
    experiment_root: Path,
    *,
    logger: logging.Logger | None = None,
) -> list[Path]:
    """
    Regenerate per-bench adaptive ramp plots and the experiment goodput chart.

    Called after each successful Locust run so plots stay current without a
    separate ``k8s-plot`` pass.
    """
    log = logger or logging.getLogger(__name__)
    created: list[Path] = []
    try:
        for plot_path in regenerate_bench_plots(bench_dir):
            created.append(plot_path)
            log.info("Updated bench plot: %s", plot_path)
        for plot_path in regenerate_experiment_plots(
            experiment_root,
            include_bench_plots=False,
        ):
            created.append(plot_path)
            log.info("Updated experiment plot: %s", plot_path)
    except Exception as exc:
        log.warning("Could not update experiment plots: %s", exc)
    return created

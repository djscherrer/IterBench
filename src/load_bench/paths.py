"""
Canonical on-disk layout for Locust artifacts within a bench run.

Diagnostics paths live in :mod:`bench_diagnostics.paths`.
"""

from __future__ import annotations

from pathlib import Path


def locust_dir(run_dir: Path) -> Path:
    """``<run_dir>/locust/`` — staged locustfile + shape live here."""
    d = run_dir / "locust"
    d.mkdir(parents=True, exist_ok=True)
    return d


def locust_results_dir(run_dir: Path) -> Path:
    """``<run_dir>/locust/results/`` — Locust ``--csv`` output destination."""
    d = locust_dir(run_dir) / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def locust_logs_dir(run_dir: Path) -> Path:
    """``<run_dir>/locust/logs/`` — master / worker stdout+stderr captures."""
    d = locust_dir(run_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def locust_csv_prefix(run_dir: Path, test: str) -> Path:
    """
    Prefix passed to ``locust --csv <prefix>``.

    Locust appends ``_stats.csv``, ``_stats_history.csv``, ``_failures.csv``,
    and ``_exceptions.csv`` to this prefix.
    """
    return locust_results_dir(run_dir) / test

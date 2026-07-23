"""Runs a Locust script against a running container.

Always local: shells out to ``locust`` headless against ``localhost:<port>``
for the real weighted-load run, and to :mod:`performance.smoke_runner` (a
separate process, for the same gevent-monkey-patching-isolation reason) for
the deterministic endpoint-coverage sweep. Neither is a real benchmark run
at cluster scale, so there's no need for the SSH-based distributed runner
load_bench/k8s_bench use for actual cluster benchmarking.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
import sys

import _bootstrap  # noqa: F401  (baxbench src/ onto sys.path)

from load_bench.locust_run import resolve_locust_user_class


def run_locust_against_container(
    *,
    locust_file: pathlib.Path,
    csv_prefix: pathlib.Path,
    target_port: int,
    logger: logging.Logger,
    run_time_s: int = 15,
    users: int = 1,
    spawn_rate: int = 1,
) -> subprocess.CompletedProcess:
    """Runs the locustfile headless for ``run_time_s`` seconds against
    localhost:target_port, writing ``<csv_prefix>_stats.csv`` (+ siblings)."""
    user_class = resolve_locust_user_class(locust_file)
    logger.info("Locust user class: %s", user_class)

    cmd = [
        "locust",
        "-f",
        str(locust_file),
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "--run-time",
        f"{run_time_s}s",
        "--host",
        f"http://localhost:{target_port}",
        "--csv",
        str(csv_prefix),
    ]
    logger.info("Running locust: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def run_smoke_against_container(
    *,
    locust_file: pathlib.Path,
    csv_prefix: pathlib.Path,
    target_port: int,
    logger: logging.Logger,
) -> subprocess.CompletedProcess:
    """Deterministically calls every distinct ``@task`` on the generated
    script's User class exactly once against localhost:target_port, writing
    ``<csv_prefix>_stats.csv`` in the same format the real run produces.

    Unlike the weighted real run, coverage here does not depend on how many
    users/how long a run gets: every task fires exactly once, so a rarely
    weighted task can't fail to be sampled within a short window.
    """
    smoke_runner_script = pathlib.Path(__file__).resolve().parent / "smoke_runner.py"
    cmd = [
        sys.executable,
        str(smoke_runner_script),
        str(locust_file),
        "--host",
        f"http://localhost:{target_port}",
        "--csv-prefix",
        str(csv_prefix),
    ]
    logger.info("Running deterministic endpoint smoke: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)

"""Runs a Locust script against a running container.

Always local: shells out to ``locust`` headless against ``localhost:<port>``.
This is a quick smoke check (does the generated script exercise every
endpoint), not a real benchmark run, so there's no need for the SSH-based
distributed runner load_bench/k8s_bench use for actual cluster benchmarking.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess

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
    smoke: bool = False,
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
    process_env = os.environ.copy()
    if smoke:
        process_env["BAXBENCH_LOCUST_SMOKE"] = "1"
    else:
        process_env.pop("BAXBENCH_LOCUST_SMOKE", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=process_env)

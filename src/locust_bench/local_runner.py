"""
Run Locust on **this machine** (not over SSH).

Used when the system under test is reachable locally — e.g. ``kubectl port-forward``
(k8s-bench) or a docker container mapped to localhost (local ``bench`` without remote hosts).
Distributed bench uses ``runner.LocustRunner`` on r630 load hosts instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .load_profiles import LoadProfile, resolve_load_profile
from .load_profiles.env import build_baxbench_locust_env

_BAXBENCH_SHAPE = Path(__file__).parent / "load_profiles" / "_baxbench_shape.py"


def resolve_locust_user_class(locustfile: Path, requested: str = "default") -> str:
    """
    Map BaxBench performance test names to Locust user class names.

    When ``performance_tests`` is empty we use ``default``; infer the first
    ``HttpUser`` / ``FastHttpUser`` subclass from the locustfile.
    """
    if requested and requested != "default":
        return requested
    text = locustfile.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"class\s+(\w+)\s*\(\s*(?:Fast)?HttpUser\s*\)",
        text,
    )
    if match:
        return match.group(1)
    return requested


def prepare_locust_run_dir(run_dir: Path, locustfile: Path) -> Path:
    """Copy locustfile and ``_baxbench_shape.py`` into ``run_dir`` for imports."""
    run_dir.mkdir(parents=True, exist_ok=True)
    dest_locust = run_dir / locustfile.name
    if locustfile.resolve() != dest_locust.resolve():
        shutil.copy2(locustfile, dest_locust)
    if _BAXBENCH_SHAPE.is_file():
        shutil.copy2(_BAXBENCH_SHAPE, run_dir / "_baxbench_shape.py")
    return dest_locust


def run_headless_locust(
    *,
    locustfile: Path,
    csv_prefix: Path,
    target_host: str,
    timeout: int,
    locust_user: str,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
    load_profile: LoadProfile | None = None,
) -> bytes:
    """
    Run Locust headless locally with BaxBench load-shape env vars.

    ``target_host`` is the full base URL (e.g. ``http://127.0.0.1:8080``).
    """
    profile = load_profile or resolve_load_profile(os.environ.get("BAXBENCH_LOAD_PROFILE", "default"))
    run_time_s = (
        int(bench_run_time) if bench_run_time is not None else int(profile.effective_run_time_s)
    )
    users = int(bench_users) if bench_users is not None else int(profile.effective_users)
    spawn_rate = (
        int(bench_spawn_rate) if bench_spawn_rate is not None else int(profile.effective_spawn_rate)
    )

    run_dir = csv_prefix.parent
    locustfile = prepare_locust_run_dir(run_dir, locustfile)
    user_class = resolve_locust_user_class(locustfile, locust_user)
    proc_env = os.environ.copy()
    proc_env.update(
        build_baxbench_locust_env(profile, bench_run_time_s=run_time_s, bench_users=users)
    )

    try:
        result = subprocess.run(
            [
                "locust",
                "--headless",
                "--locustfile",
                str(locustfile),
                "--host",
                target_host,
                "--users",
                str(users),
                "--spawn-rate",
                str(spawn_rate),
                "--run-time",
                f"{run_time_s}s",
                "--csv",
                str(csv_prefix),
                "--csv-full-history",
                "--only-summary",
                user_class,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=proc_env,
            cwd=str(run_dir),
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        raise TimeoutError("Benchmarking timed out") from None

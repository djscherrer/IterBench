from __future__ import annotations

import re
from dataclasses import replace
from math import ceil

from .models import (
    ContinuousLoadProfile,
    LoadProfile,
    SpikeLoadProfile,
    StairsLoadProfile,
    SteadyLoadProfile,
)


LOAD_PROFILE_REGISTRY: dict[str, LoadProfile] = {
    # Aligned with current bench.sh defaults for predictable behavior.
    "default": SteadyLoadProfile(
        name="default", run_time_s=180, wait_min_s=1.0, wait_max_s=1.0, locust_processes=8, users=1000
    ),
    "quick-check": SteadyLoadProfile(
        name="quick-check", users=200, run_time_s=30, wait_min_s=1.0, wait_max_s=1.0, locust_processes=8
    ),
    "cont-20000": ContinuousLoadProfile(
        name="cont-20000",
        start_users=0,
        target_users=2000,
        spawn_rate=20,
        run_time_s=240,
        wait_min_s=0,
        wait_max_s=0,
        locust_processes=8,
    ),
    "stairs-100-100-30-10": StairsLoadProfile(
        name="stairs-100-100-30-10",
        start_users=100,
        step_users=100,
        step_duration_s=30,
        steps=10,
        run_time_s=300,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),
    "spike-500-1000-30-10": SpikeLoadProfile(
        name="spike-500-1000-30-10",
        base_users=500,
        spike_users=1000,
        interval_s=30,
        duration_s=10,
        run_time_s=300,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),

    "stairs-stress": StairsLoadProfile(
        name="stairs-stress",
        start_users=200,
        step_users=300,
        step_duration_s=30,
        steps=10, # Goes up to 3200 RPS
        run_time_s=300,
        wait_min_s=1,
        wait_max_s=1,
        locust_processes=8,
    ),

    "stairs-fine-stress": StairsLoadProfile(
        name="stairs-fine-stress",
        start_users=600,
        step_users=100,
        step_duration_s=45,
        steps=15, # Goes from 600 up to 2000 users
        run_time_s=675, # 15 steps * 45 seconds
        wait_min_s=1,
        wait_max_s=1,
        locust_processes=8,
    ),

    "stairs-fine-stress-fast": StairsLoadProfile(
        name="stairs-fine-stress-fast",
        start_users=1200,
        step_users=100,
        step_duration_s=30,
        steps=7, # Goes from 1200 up to 1600 users
        run_time_s=210, # 15 steps * 45 seconds
        wait_min_s=1,
        wait_max_s=1,
        locust_processes=8,
    ),

    # This profile should saturate the openhands performant ClickCount backends for all environments
    # Given "2C-1B-1DB" & load_workers=("r630-06",) * 16 + ("r630-12",) * 16,
    "stairs-1400-200-20-10": StairsLoadProfile(
        name="stairs-1400-200-20-10",
        start_users=1400,
        step_users=200,
        step_duration_s=20,
        steps=10,
        run_time_s=200,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),

    # This profile should saturate the openhands performant Recipes backends for Go-net/http
    # Given "2C-1B-1DB" & load_workers=("r630-06",) * 16 + ("r630-12",) * 16,
    "stairs-8400-200-20-10": StairsLoadProfile(
        name="stairs-8400-200-20-10",
        start_users=8400,
        step_users=200,
        step_duration_s=20,
        steps=10,
        run_time_s=200,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),

    "stairs-massive-microblog": StairsLoadProfile(
        name="stairs-massive-microblog",
        start_users=3000,
        step_users=1500,
        step_duration_s=30,
        steps=12, # Up to 18000 users
        run_time_s=360,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),

    "stairs-aggressive-microblog": StairsLoadProfile(
        name="stairs-aggressive-microblog",
        start_users=5000,
        step_users=2500,
        step_duration_s=30,
        steps=10, # Up to 30000 users
        run_time_s=300,
        wait_min_s=0.2,
        wait_max_s=0.5,
        locust_processes=16,
    ),

}


_DYNAMIC_STAIRS_RE = re.compile(r"^stairs-(\d+)-(\d+)-(\d+)-(\d+)$")


def _resolve_dynamic_load_profile(name: str) -> LoadProfile | None:
    m = _DYNAMIC_STAIRS_RE.fullmatch(name)
    if not m:
        return None
    start_users, step_users, step_duration_s, steps = (int(x) for x in m.groups())
    return StairsLoadProfile(
        name=name,
        start_users=start_users,
        step_users=step_users,
        step_duration_s=step_duration_s,
        steps=steps,
        run_time_s=step_duration_s * steps,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    )


def resolve_load_profile(name: str | None) -> LoadProfile:
    key = (name or "default").strip()
    if key not in LOAD_PROFILE_REGISTRY:
        dynamic = _resolve_dynamic_load_profile(key)
        if dynamic is not None:
            return dynamic
        known = ", ".join(sorted(LOAD_PROFILE_REGISTRY))
        raise ValueError(
            f"Unknown load profile '{key}'. Known profiles: {known}. "
            "Dynamic stair profiles are also supported as "
            "stairs-<start_users>-<step_users>-<step_duration_s>-<steps>."
        )
    return LOAD_PROFILE_REGISTRY[key]


def merge_load_profile_with_overrides(
    profile: LoadProfile,
    *,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    locust_processes: int | None,
) -> LoadProfile:
    run_time_s = int(bench_run_time) if bench_run_time is not None else int(profile.run_time_s)
    if isinstance(profile, StairsLoadProfile) and run_time_s <= 0:
        run_time_s = int(profile.step_duration_s * profile.steps)
    elif run_time_s <= 0:
        run_time_s = int(profile.run_time_s)
    common_updates = {
        "run_time_s": int(run_time_s),
        "wait_min_s": float(profile.wait_min_s),
        "wait_max_s": float(profile.wait_max_s),
        "locust_processes": int(locust_processes) if locust_processes is not None else int(profile.locust_processes),
    }
    if isinstance(profile, SteadyLoadProfile):
        return replace(
            profile,
            users=int(bench_users) if bench_users is not None else int(profile.users),
            **common_updates,
        )
    if isinstance(profile, ContinuousLoadProfile):
        return replace(
            profile,
            target_users=int(bench_users) if bench_users is not None else int(profile.target_users),
            spawn_rate=int(bench_spawn_rate) if bench_spawn_rate is not None else int(profile.spawn_rate),
            **common_updates,
        )
    if isinstance(profile, StairsLoadProfile):
        steps = int(profile.steps)
        if bench_users is not None and int(profile.step_users) > 0:
            needed_delta = max(0, int(bench_users) - int(profile.start_users))
            steps = max(1, ceil(needed_delta / int(profile.step_users)))
        return replace(
            profile,
            steps=steps,
            **common_updates,
        )
    if isinstance(profile, SpikeLoadProfile):
        return replace(
            profile,
            spike_users=int(bench_users) if bench_users is not None else int(profile.spike_users),
            **common_updates,
        )
    raise TypeError(f"Unsupported load profile type: {type(profile)!r}")

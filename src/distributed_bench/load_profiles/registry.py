from __future__ import annotations

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
    "stairs-500-100-30-10": StairsLoadProfile(
        name="stairs-500-100-30-10",
        start_users=500,
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
}


def resolve_load_profile(name: str | None) -> LoadProfile:
    key = (name or "default").strip()
    if key not in LOAD_PROFILE_REGISTRY:
        known = ", ".join(sorted(LOAD_PROFILE_REGISTRY))
        raise ValueError(f"Unknown load profile '{key}'. Known profiles: {known}")
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

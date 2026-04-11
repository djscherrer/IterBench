from __future__ import annotations

from .models import LoadProfile


LOAD_PROFILE_REGISTRY: dict[str, LoadProfile] = {
    # Aligned with current bench.sh defaults for predictable behavior.
    "default": LoadProfile(
        name="default",
        users=1000,
        spawn_rate=20,
        run_time_s=180,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),
    "quick-check": LoadProfile(name="quick-check", users=200, spawn_rate=20, run_time_s=60, wait_min_s=1.0, wait_max_s=1.0),
    "stress-heavy": LoadProfile(name="stress-heavy", users=1500, spawn_rate=30, run_time_s=300, wait_min_s=0.5, wait_max_s=1.0),
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
    return LoadProfile(
        name=profile.name,
        users=int(bench_users) if bench_users is not None else int(profile.users),
        spawn_rate=int(bench_spawn_rate) if bench_spawn_rate is not None else int(profile.spawn_rate),
        run_time_s=int(bench_run_time) if bench_run_time is not None else int(profile.run_time_s),
        wait_min_s=float(profile.wait_min_s),
        wait_max_s=float(profile.wait_max_s),
        locust_processes=int(locust_processes) if locust_processes is not None else int(profile.locust_processes),
        extra_locust_args=tuple(profile.extra_locust_args),
    )

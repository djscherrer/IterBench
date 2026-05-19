"""Environment variables consumed by ``_baxbench_shape.py``."""

from __future__ import annotations

from .models import (
    AdaptiveLoadProfile,
    ContinuousLoadProfile,
    LoadProfile,
    SpikeLoadProfile,
    StairsLoadProfile,
)


def build_baxbench_locust_env(
    load_profile: LoadProfile,
    *,
    bench_run_time_s: int,
    bench_users: int | None = None,
) -> dict[str, str]:
    if isinstance(load_profile, AdaptiveLoadProfile):
        load_mode = "adaptive"
    elif isinstance(load_profile, ContinuousLoadProfile):
        load_mode = "continuous"
    elif isinstance(load_profile, StairsLoadProfile):
        load_mode = "stairs"
    elif isinstance(load_profile, SpikeLoadProfile):
        load_mode = "spike"
    else:
        load_mode = "steady"

    env: dict[str, str] = {
        "BAXBENCH_LOCUST_WAIT_MIN_S": str(load_profile.wait_min_s),
        "BAXBENCH_LOCUST_WAIT_MAX_S": str(load_profile.wait_max_s),
        "BAXBENCH_LOAD_MODE": load_mode,
        "BAXBENCH_RUN_TIME_S": str(int(bench_run_time_s)),
    }

    if load_mode == "steady":
        users = int(bench_users if bench_users is not None else load_profile.effective_users)
        env["BAXBENCH_STEADY_USERS"] = str(users)
    elif load_mode == "continuous" and isinstance(load_profile, ContinuousLoadProfile):
        env["BAXBENCH_CONTINUOUS_SPAWN_RATE"] = str(int(load_profile.spawn_rate))
        env["BAXBENCH_CONTINUOUS_START_USERS"] = str(int(load_profile.start_users))
        env["BAXBENCH_CONTINUOUS_TARGET_USERS"] = str(int(load_profile.target_users))
    elif load_mode == "stairs" and isinstance(load_profile, StairsLoadProfile):
        env["BAXBENCH_STAIRS_START_USERS"] = str(int(load_profile.start_users))
        env["BAXBENCH_STAIRS_STEP_USERS"] = str(int(load_profile.step_users))
        env["BAXBENCH_STAIRS_STEP_DURATION_S"] = str(int(load_profile.step_duration_s))
        env["BAXBENCH_STAIRS_STEPS"] = str(int(load_profile.steps))
    elif load_mode == "spike" and isinstance(load_profile, SpikeLoadProfile):
        env["BAXBENCH_SPIKE_BASE_USERS"] = str(int(load_profile.base_users))
        env["BAXBENCH_SPIKE_USERS"] = str(int(load_profile.spike_users))
        env["BAXBENCH_SPIKE_INTERVAL_S"] = str(int(load_profile.interval_s))
        env["BAXBENCH_SPIKE_DURATION_S"] = str(int(load_profile.duration_s))
    elif load_mode == "adaptive" and isinstance(load_profile, AdaptiveLoadProfile):
        env["BAXBENCH_ADAPTIVE_SLA_MS"] = str(float(load_profile.sla_ms))
        env["BAXBENCH_ADAPTIVE_START_USERS"] = str(int(load_profile.start_users))
        env["BAXBENCH_ADAPTIVE_MAX_USERS"] = str(int(load_profile.max_users))
        env["BAXBENCH_ADAPTIVE_MIN_STEP_USERS"] = str(int(load_profile.min_step_users))
        env["BAXBENCH_ADAPTIVE_MAX_STEP_USERS"] = str(int(load_profile.max_step_users))
        env["BAXBENCH_ADAPTIVE_STEP_DURATION_S"] = str(int(load_profile.step_duration_s))
        env["BAXBENCH_ADAPTIVE_TRIM_S"] = str(int(load_profile.trim_s))
        env["BAXBENCH_ADAPTIVE_SAMPLE_EVERY_S"] = str(int(load_profile.sample_every_s))
        env["BAXBENCH_ADAPTIVE_SETTLE_SAMPLES"] = str(int(load_profile.settle_samples))
        env["BAXBENCH_ADAPTIVE_QUANTILE"] = str(float(load_profile.quantile))

    return env


def format_baxbench_locust_env_shell(
    load_profile: LoadProfile,
    *,
    bench_run_time_s: int,
    bench_users: int | None = None,
) -> str:
    """Space-separated ``KEY=value`` prefix for remote shell commands."""
    parts = [f"{k}={v}" for k, v in build_baxbench_locust_env(
        load_profile, bench_run_time_s=bench_run_time_s, bench_users=bench_users
    ).items()]
    return " ".join(parts) + " "

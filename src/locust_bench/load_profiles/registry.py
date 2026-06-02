from __future__ import annotations

from dataclasses import replace
from math import ceil

from .models import (
    AdaptiveLoadProfile,
    AdaptiveV2LoadProfile,
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

    "k8s-adaptive": AdaptiveLoadProfile(
        name="k8s-adaptive",
        sla_ms=300,
        start_users=50,
        max_users=10000,
        min_step_users=25,
        max_step_users=500,
        spawn_rate=50,
        step_duration_s=30,
        trim_s=10,
        sample_every_s=5,
        settle_samples=3,
        quantile=0.95,
        run_time_s=180,        
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=32,
    ),

    "k8s-adaptive-v2": AdaptiveV2LoadProfile(
        name="k8s-adaptive-v2",
        sla_ms=300,
        failure_threshold_pct=5.0,
        start_users=100,
        max_users=10_000,
        min_step_users=25,
        max_step_users=500,
        spawn_rate=200,
        warmup_step_duration_s=180,
        min_step_duration_s=15,
        max_step_duration_s=30,
        trim_s=5,
        sample_every_s=2,
        min_settle_samples=3,
        quantile=0.95,
        stability_cv_threshold=0.25,
        plateau_stop_steps=3,
        plateau_goodput_threshold_pct=5.0,
        run_time_s=420,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=32,
    ),

    # Load profiles for the BaxBench benchmark.
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
    "stairs-800-100-30-12": StairsLoadProfile(
        name="stairs-800-100-30-12",
        start_users=800,
        step_users=100,
        step_duration_s=30,
        steps=12,
        run_time_s=360,
        wait_min_s=1.0,
        wait_max_s=1.0,
        locust_processes=8,
    ),
    "stairs-1500-100-30-15": StairsLoadProfile(
        name="stairs-1500-100-30-15",
        start_users=1500,
        step_users=100,
        step_duration_s=30,
        steps=15,
        run_time_s=450,
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

"""Serialize load profiles to a JSON manifest consumed by Locust shapes."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import (
    AdaptiveLoadProfile,
    AdaptiveV2LoadProfile,
    ContinuousLoadProfile,
    ExploreRefineLoadProfile,
    GoodputPlateauLoadProfile,
    LoadProfile,
    SpikeLoadProfile,
    StairsLoadProfile,
    SteadyLoadProfile,
)

LOAD_PROFILE_MANIFEST_FILENAME = "baxbench_load_profile.json"
MANIFEST_VERSION = 1


def load_profile_mode(load_profile: LoadProfile) -> str:
    if isinstance(load_profile, AdaptiveV2LoadProfile):
        return "adaptive_v2"
    if isinstance(load_profile, GoodputPlateauLoadProfile):
        return "goodput_plateau"
    if isinstance(load_profile, ExploreRefineLoadProfile):
        return "explore_refine"
    if isinstance(load_profile, AdaptiveLoadProfile):
        return "adaptive"
    if isinstance(load_profile, ContinuousLoadProfile):
        return "continuous"
    if isinstance(load_profile, StairsLoadProfile):
        return "stairs"
    if isinstance(load_profile, SpikeLoadProfile):
        return "spike"
    return "steady"


def _adaptive_trim_s(load_profile: LoadProfile) -> int:
    if isinstance(load_profile, ExploreRefineLoadProfile):
        return max(0, int(load_profile.refine_trim_s))
    if isinstance(
        load_profile,
        (AdaptiveLoadProfile, AdaptiveV2LoadProfile, GoodputPlateauLoadProfile),
    ):
        return max(0, int(load_profile.trim_s))
    return 10


def _shape_runtime_extras(load_profile: LoadProfile) -> dict[str, Any]:
    """Fields used by adaptive shapes but not stored on profile dataclasses."""
    if isinstance(load_profile, ExploreRefineLoadProfile):
        return {}
    if isinstance(
        load_profile,
        (
            AdaptiveLoadProfile,
            AdaptiveV2LoadProfile,
            GoodputPlateauLoadProfile,
        ),
    ):
        trim_s = _adaptive_trim_s(load_profile)
        return {
            "health_grace_s": max(15, trim_s),
            "abort_on_no_users": True,
            "spawn_target_duration_s": 10.0,
            "spawn_settle_buffer_s": 2.0,
        }
    return {}


def build_load_profile_manifest(
    load_profile: LoadProfile,
    *,
    bench_run_time_s: int,
    bench_users: int | None = None,
) -> dict[str, Any]:
    """Build the JSON manifest written next to the staged locustfile."""
    data: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "mode": load_profile_mode(load_profile),
        "run_time_s": int(bench_run_time_s),
        **asdict(load_profile),
    }
    data.update(_shape_runtime_extras(load_profile))
    if isinstance(load_profile, SteadyLoadProfile) and bench_users is not None:
        data["steady_users"] = int(bench_users)
    return data


def write_load_profile_manifest(
    path: Path,
    load_profile: LoadProfile,
    *,
    bench_run_time_s: int,
    bench_users: int | None = None,
) -> Path:
    manifest = build_load_profile_manifest(
        load_profile,
        bench_run_time_s=bench_run_time_s,
        bench_users=bench_users,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def resolved_profile_from_bench_config(config: dict) -> dict[str, Any] | None:
    """Return the staged load profile manifest embedded in bench ``config.json``."""
    resolved = config.get("resolved_load_profile")
    if isinstance(resolved, dict) and resolved.get("mode"):
        return resolved
    return None

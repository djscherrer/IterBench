from __future__ import annotations

import json
from pathlib import Path

_LOAD_PROFILE_MANIFEST = "baxbench_load_profile.json"

_manifest: dict | None = None

_EXPLORE_REFINE_REQUIRED_KEYS: tuple[str, ...] = (
    "failure_threshold_pct",
    "overload_p95_ms",
    "start_users",
    "max_users",
    "run_time_s",
    "sample_every_s",
    "quantile",
    "explore_warmup_duration_s",
    "explore_ramp_user_fraction_per_s",
    "explore_min_step_users",
    "explore_goodput_stop_ratio",
    "explore_stop_steps",
    "recovery_floor_fraction",
    "recovery_settle_duration_s",
    "recovery_retry_drop_fraction",
    "recovery_max_retries",
    "refine_max_step_duration_s",
    "refine_min_settle_samples",
    "refine_trim_s",
    "refine_min_step_users",
    "refine_max_step_fraction",
    "refine_goodput_stability_pct",
)


def _manifest_required(cfg: dict, key: str):
    if key not in cfg:
        mode = cfg.get("mode", "?")
        raise KeyError(
            f"load profile manifest missing required key {key!r} (mode={mode!r})"
        )
    return cfg[key]


def _validate_explore_refine_manifest(cfg: dict) -> None:
    missing = [k for k in _EXPLORE_REFINE_REQUIRED_KEYS if k not in cfg]
    if missing:
        raise KeyError(
            "explore_refine manifest missing required keys: "
            + ", ".join(sorted(missing))
        )


def _load_manifest() -> dict:
    global _manifest
    if _manifest is not None:
        return _manifest
    here = Path(__file__).resolve().parent
    candidates = [
        # Staged next to locustfile, with shapes/ as a sibling package:
        here.parent / _LOAD_PROFILE_MANIFEST,
        # Manifest dropped inside shapes/ (unusual):
        here / _LOAD_PROFILE_MANIFEST,
        Path.cwd() / _LOAD_PROFILE_MANIFEST,
        Path.cwd() / "locust" / _LOAD_PROFILE_MANIFEST,
    ]
    for path in candidates:
        if path.is_file():
            _manifest = json.loads(path.read_text(encoding="utf-8"))
            return _manifest
    raise FileNotFoundError(
        f"Missing {_LOAD_PROFILE_MANIFEST} beside the locustfile. "
        "Stage it with prepare_locust_run_dir()."
    )


def reset_manifest_cache() -> None:
    """Test helper: clear cached manifest."""
    global _manifest
    _manifest = None

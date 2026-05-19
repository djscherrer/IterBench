from __future__ import annotations

import re
from pathlib import Path

K8S_CONFIGS_DIRNAME = "k8s_configs"
ITERATION_PREFIX = "iteration-"


def k8s_configs_root(sample_dir: Path) -> Path:
    return sample_dir / K8S_CONFIGS_DIRNAME


def iteration_dir(sample_dir: Path, iteration_id: str) -> Path:
    iteration_id = normalize_iteration_id(iteration_id)
    return k8s_configs_root(sample_dir) / iteration_id


def iteration_spec_path(iteration_path: Path) -> Path:
    return iteration_path / "spec.yaml"


def iteration_manifests_dir(iteration_path: Path) -> Path:
    return iteration_path / "manifests"


def deploy_record_path(iteration_path: Path) -> Path:
    return iteration_path / "deploy.json"


def normalize_iteration_id(raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip()).strip("-").lower()
    if not slug:
        raise ValueError("iteration id must not be empty")
    if slug.startswith(ITERATION_PREFIX):
        return slug
    if slug.isdigit():
        return f"{ITERATION_PREFIX}{slug.zfill(3)}"
    return f"{ITERATION_PREFIX}{slug}"


def iteration_id_for_phase(phase_index: int) -> str:
    """1-based phase index → ``iteration-001``, ``iteration-002``, …"""
    if phase_index < 1:
        raise ValueError("phase_index must be >= 1")
    return f"{ITERATION_PREFIX}{phase_index:03d}"


def new_iteration_id(sample_dir: Path) -> str:
    """Return the next ``iteration-NNN`` id under ``sample_dir/k8s_configs``."""
    root = k8s_configs_root(sample_dir)
    root.mkdir(parents=True, exist_ok=True)
    max_n = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = re.fullmatch(r"iteration-(\d+)", child.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{ITERATION_PREFIX}{max_n + 1:03d}"


def list_iteration_dirs(sample_dir: Path) -> list[Path]:
    root = k8s_configs_root(sample_dir)
    if not root.is_dir():
        return []
    dirs = [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and iteration_spec_path(child).is_file()
    ]
    return dirs


def perf_run_dir_for_iteration(
    sample_dir: Path,
    iteration_id: str,
    *,
    load_profile: str,
    timestamp: str,
) -> Path:
    """
    Per-run output when benchmarking a specific K8s iteration.

    Example: ``sample1/perf-k8s-iteration-001-stairs-800-20260517-120000``
    """
    iid = normalize_iteration_id(iteration_id)
    safe_profile = re.sub(r"[^a-zA-Z0-9_-]+", "-", load_profile.strip()) or "default"
    return sample_dir / f"perf-k8s-{iid}-{safe_profile}-{timestamp}"

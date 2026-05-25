from __future__ import annotations

import os
import re
from pathlib import Path

K8S_CONFIGS_DIRNAME = "k8s_configs"
K8S_EXPERIMENTS_DIRNAME = "k8s-experiments"
ITERATION_PREFIX = "iteration-"


def normalize_experiment_id(raw: str) -> str:
    """Filesystem-safe experiment slug (e.g. ``experiment-a``)."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip()).strip("-").lower()
    if not slug:
        raise ValueError("experiment id must not be empty")
    return slug


def resolve_k8s_experiment_id() -> str | None:
    """
    Active experiment from ``BAXBENCH_K8S_EXPERIMENT``, or ``None`` for legacy layout.

    Legacy (no experiment): ``sampleN/k8s_configs/`` and ``sampleN/perf-k8s-…/``.
    With experiment: ``sampleN/k8s-experiments/<slug>/k8s_configs/`` and perf runs
    under the same ``k8s-experiments/<slug>/`` directory.
    """
    value = os.environ.get("BAXBENCH_K8S_EXPERIMENT", "").strip()
    if not value:
        return None
    return normalize_experiment_id(value)


def k8s_workspace_root(sample_dir: Path) -> Path:
    """Root for one experiment's configs + perf runs (or ``sample_dir`` when unset)."""
    eid = resolve_k8s_experiment_id()
    if not eid:
        return sample_dir
    return sample_dir / K8S_EXPERIMENTS_DIRNAME / eid


def default_k8s_namespace(iteration_id: str) -> str:
    """Kubernetes namespace for an iteration (includes experiment slug when set)."""
    iid = normalize_iteration_id(iteration_id)
    eid = resolve_k8s_experiment_id()
    if eid:
        return f"baxbench-{eid}-{iid}"
    return f"baxbench-{iid}"


def k8s_configs_root(sample_dir: Path) -> Path:
    return k8s_workspace_root(sample_dir) / K8S_CONFIGS_DIRNAME


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

    Example (legacy): ``sample1/perf-k8s-iteration-001-stairs-800-20260517-120000``

    With experiment ``exp-a``:
    ``sample1/k8s-experiments/exp-a/perf-k8s-iteration-001-…``
    """
    iid = normalize_iteration_id(iteration_id)
    safe_profile = re.sub(r"[^a-zA-Z0-9_-]+", "-", load_profile.strip()) or "default"
    workspace = k8s_workspace_root(sample_dir)
    return workspace / f"perf-k8s-{iid}-{safe_profile}-{timestamp}"

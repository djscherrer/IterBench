from __future__ import annotations

import os
import re
from pathlib import Path

K8S_EXPERIMENTS_DIRNAME = "k8s-experiments"
ITERATIONS_DIRNAME = "iterations"
ITERATION_PREFIX = "iteration-"
DEFAULT_EXPERIMENT_SLUG = "default"
ITERATION_KIND_SUFFIXES = frozenset({"baseline", "spec", "code"})
_ITERATION_FOLDER_RE = re.compile(
    r"^iteration-(\d{3})(?:-(baseline|spec|code))?(?:-failed)?$"
)


def normalize_experiment_id(raw: str) -> str:
    """Filesystem-safe experiment slug (e.g. ``experiment-a``)."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip()).strip("-").lower()
    if not slug:
        raise ValueError("experiment id must not be empty")
    return slug


def resolve_k8s_experiment_id() -> str:
    """Active experiment slug from ``BAXBENCH_K8S_EXPERIMENT``, else ``default``."""
    value = os.environ.get("BAXBENCH_K8S_EXPERIMENT", "").strip()
    if value:
        return normalize_experiment_id(value)
    return DEFAULT_EXPERIMENT_SLUG


def k8s_workspace_root(sample_dir: Path) -> Path:
    """Root for one experiment: ``sampleN/k8s-experiments/<slug>/``."""
    return sample_dir / K8S_EXPERIMENTS_DIRNAME / resolve_k8s_experiment_id()


def iterations_root(sample_dir: Path) -> Path:
    return k8s_workspace_root(sample_dir) / ITERATIONS_DIRNAME


def normalize_iteration_id(raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip()).strip("-").lower()
    if not slug:
        raise ValueError("iteration id must not be empty")
    if slug.startswith(ITERATION_PREFIX):
        return slug
    if slug.isdigit():
        return f"{ITERATION_PREFIX}{slug.zfill(3)}"
    return f"{ITERATION_PREFIX}{slug}"


def parse_iteration_folder_name(name: str) -> tuple[int | None, str | None, bool]:
    """
    Parse folder names such as ``iteration-001``, ``iteration-007-spec``,
    ``iteration-008-code-failed``.

    Returns ``(None, None, False)`` when the name is not recognized.
    """
    m = _ITERATION_FOLDER_RE.fullmatch(name)
    if not m:
        return None, None, False
    failed = name.endswith("-failed")
    return int(m.group(1)), m.group(2), failed


def parse_iteration_phase(folder_name: str) -> int | None:
    phase, _kind, _failed = parse_iteration_folder_name(folder_name)
    return phase


def iteration_folder_is_failed(name: str) -> bool:
    """True when the folder name ends with ``-failed``."""
    _, _, failed = parse_iteration_folder_name(name)
    return failed


def iteration_folder_with_suffix(phase_slug: str, kind: str | None) -> str:
    """Build ``iteration-007-spec`` or ``iteration-001-baseline``."""
    if not phase_slug.isdigit():
        raise ValueError(f"invalid phase slug: {phase_slug!r}")
    base = f"{ITERATION_PREFIX}{phase_slug.zfill(3)}"
    if kind is None:
        return base
    if kind not in ITERATION_KIND_SUFFIXES:
        raise ValueError(f"invalid iteration kind: {kind!r}")
    return f"{base}-{kind}"


def apply_iteration_folder_suffix(iteration_path: Path, kind: str) -> Path:
    """
    Rename ``iteration-005`` → ``iteration-005-spec`` (or ``-code`` / ``-baseline``).

    No-op when the folder already uses the requested suffix or is marked failed.
    """
    phase, current_kind, failed = parse_iteration_folder_name(iteration_path.name)
    if phase is None:
        return iteration_path
    if failed:
        return iteration_path
    if current_kind == kind:
        return iteration_path
    target = iteration_path.parent / iteration_folder_with_suffix(
        str(phase).zfill(3), kind
    )
    if target == iteration_path:
        return iteration_path
    if target.exists():
        raise FileExistsError(
            f"Cannot rename {iteration_path.name!r} to {target.name!r}: target exists"
        )
    iteration_path.rename(target)
    return target


def mark_iteration_folder_failed(iteration_path: Path) -> Path:
    """
    Rename ``iteration-003-spec`` → ``iteration-003-spec-failed``.

    Failed folders are excluded from the feedback chain.
    """
    phase, kind, failed = parse_iteration_folder_name(iteration_path.name)
    if phase is None or failed:
        return iteration_path
    slug = str(phase).zfill(3)
    if kind:
        target_name = iteration_folder_with_suffix(slug, kind) + "-failed"
    else:
        target_name = f"{ITERATION_PREFIX}{slug}-failed"
    target = iteration_path.parent / target_name
    if target == iteration_path:
        return iteration_path
    if target.exists():
        raise FileExistsError(
            f"Cannot mark failed: {target.name!r} already exists"
        )
    iteration_path.rename(target)
    return target


def iteration_dir(sample_dir: Path, iteration_id: str) -> Path:
    """Canonical write path before kind suffix: ``…/iterations/iteration-001``."""
    return iterations_root(sample_dir) / normalize_iteration_id(iteration_id)


def resolve_iteration_dir(
    sample_dir: Path,
    iteration_id: str,
    *,
    exclude_failed: bool = True,
) -> Path:
    """Return the best-matching iteration directory for a logical iteration id."""
    iid = normalize_iteration_id(iteration_id)
    m = re.fullmatch(r"iteration-(\d+)", iid)
    if not m:
        return iteration_dir(sample_dir, iteration_id)

    phase_slug = m.group(1).zfill(3)
    prefix = f"{ITERATION_PREFIX}{phase_slug}"
    root = iterations_root(sample_dir)
    candidates: list[Path] = []
    if root.is_dir():
        exact = root / prefix
        if exact.is_dir() or _iteration_has_artifacts(exact):
            candidates.append(exact)
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith(f"{prefix}-") or child.name == prefix:
                if _iteration_has_artifacts(child) or child.is_dir():
                    candidates.append(child)
    if exclude_failed:
        non_failed = [
            c for c in candidates if not iteration_folder_is_failed(c.name)
        ]
        if non_failed:
            candidates = non_failed
    if candidates:
        return max(candidates, key=lambda p: (p.stat().st_mtime, len(p.name)))

    return root / prefix


def _iteration_has_artifacts(path: Path) -> bool:
    if not path:
        return False
    bench = path / "bench"
    return any(
        p.is_file()
        for p in (
            path / "meta.json",
            path / "spec" / "spec.yaml",
            bench / "config.json",
            bench / "iteration_feedback.json",
        )
    )


def iteration_meta_path(iteration_path: Path) -> Path:
    return iteration_path / "meta.json"


def iteration_spec_path(iteration_path: Path) -> Path:
    return iteration_path / "spec" / "spec.yaml"


def find_iteration_spec_path(iteration_path: Path) -> Path | None:
    spec_path = iteration_spec_path(iteration_path)
    return spec_path if spec_path.is_file() else None


def require_iteration_spec_path(iteration_path: Path) -> Path:
    spec_path = find_iteration_spec_path(iteration_path)
    if spec_path is not None:
        return spec_path
    raise FileNotFoundError(
        f"Missing spec for {iteration_path}; expected {iteration_spec_path(iteration_path)}"
    )


def iteration_spec_dir(iteration_path: Path) -> Path:
    return iteration_path / "spec"


def iteration_manifests_dir(iteration_path: Path) -> Path:
    return iteration_path / "manifests"


def deploy_probe_record_path(iteration_path: Path) -> Path:
    return iteration_path / "deploy" / "probe.json"


def deploy_bench_record_path(iteration_path: Path) -> Path:
    return iteration_path / "deploy" / "bench.json"


def iteration_decision_dir(iteration_path: Path) -> Path:
    return iteration_path / "decision"


def iteration_code_snapshot_dir(iteration_path: Path) -> Path:
    return iteration_path / "code"


def iteration_functional_tests_dir(iteration_path: Path) -> Path:
    return iteration_path / "functional_tests"


def iteration_bench_dir(iteration_path: Path) -> Path:
    return iteration_path / "bench"


def default_k8s_namespace(iteration_id: str) -> str:
    """Kubernetes namespace for an iteration (includes experiment slug)."""
    iid = normalize_iteration_id(iteration_id)
    eid = resolve_k8s_experiment_id()
    return f"baxbench-{eid}-{iid}"


def iteration_id_for_phase(phase_index: int) -> str:
    """0-based phase index → ``iteration-000`` (baseline), ``iteration-001``, …"""
    if phase_index < 0:
        raise ValueError("phase_index must be >= 0")
    return f"{ITERATION_PREFIX}{phase_index:03d}"


def is_baseline_phase(phase_index: int) -> bool:
    return phase_index == 0


def _max_iteration_number(sample_dir: Path) -> int:
    root = iterations_root(sample_dir)
    if not root.is_dir():
        return 0
    max_n = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        phase = parse_iteration_phase(child.name)
        if phase is not None:
            max_n = max(max_n, phase)
    return max_n


def new_iteration_id(sample_dir: Path) -> str:
    """Return the next ``iteration-NNN`` id."""
    iterations_root(sample_dir).mkdir(parents=True, exist_ok=True)
    return f"{ITERATION_PREFIX}{_max_iteration_number(sample_dir) + 1:03d}"


def list_iteration_dirs(sample_dir: Path) -> list[Path]:
    """List successful iteration directories that contain a spec."""
    root = iterations_root(sample_dir)
    if not root.is_dir():
        return []

    found: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        phase = parse_iteration_phase(child.name)
        if phase is None:
            continue
        if iteration_folder_is_failed(child.name):
            continue
        iid = iteration_id_for_phase(phase)
        if find_iteration_spec_path(child) is not None:
            found[iid] = child

    return [
        p
        for _, p in sorted(
            found.items(),
            key=lambda item: parse_iteration_phase(item[1].name) or 0,
        )
    ]


def _bench_run_complete(bench: Path) -> bool:
    return (bench / "config.json").is_file() or (
        bench / "iteration_feedback.json"
    ).is_file()


def resolve_bench_dir(sample_dir: Path, iteration_id: str) -> Path | None:
    """Return ``iterations/<id>/bench`` when a finished run exists."""
    ip = resolve_iteration_dir(sample_dir, iteration_id)
    bench = iteration_bench_dir(ip)
    if _bench_run_complete(bench):
        return bench
    return None


def perf_run_dir_for_iteration(
    sample_dir: Path,
    iteration_id: str,
    *,
    load_profile: str,
    timestamp: str,
) -> Path:
    del load_profile, timestamp
    ip = resolve_iteration_dir(sample_dir, iteration_id)
    from .layout import ensure_iteration_core_layout

    ensure_iteration_core_layout(ip)
    return iteration_bench_dir(ip)

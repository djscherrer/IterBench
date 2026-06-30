from __future__ import annotations

import re
from pathlib import Path

K8S_EXPERIMENTS_DIRNAME = "k8s-experiments"
ITERATIONS_DIRNAME = "iterations"
PLOTS_DIRNAME = "plots"
ITERATION_PREFIX = "iteration-"
DEFAULT_EXPERIMENT_SLUG = "default"
ITERATION_KIND_SUFFIXES = frozenset({"baseline", "spec", "code"})
_ITERATION_FOLDER_RE = re.compile(
    r"^iteration-(\d{3})(?:-(baseline|spec|code))?(?:-failed)?$"
)

# Numbered phase folders inside one iteration directory. The numeric prefix
# encodes execution order: decision → code (optional) → spec → deploy → bench.
# This makes ``ls iteration-007-code/`` self-documenting and lets a human (or a
# parser) tell *where* an iteration failed by inspecting which phase folder is
# the last one populated. ``failure_report.json`` lives next to the phase that
# produced it (e.g. ``02-code/failure_report.json`` for an FT failure).
PHASE_DECISION_DIRNAME = "01-decision"
PHASE_CODE_DIRNAME = "02-code"
PHASE_SPEC_DIRNAME = "03-spec"
PHASE_DEPLOY_DIRNAME = "04-deploy"
PHASE_BENCH_DIRNAME = "05-bench"


def normalize_experiment_id(raw: str) -> str:
    """Filesystem-safe experiment slug (e.g. ``experiment-a``)."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip()).strip("-").lower()
    if not slug:
        raise ValueError("experiment id must not be empty")
    return slug


def resolve_k8s_experiment_id(experiment_id: str | None = None) -> str:
    """Normalize an experiment slug; ``None`` or empty → ``default``."""
    if experiment_id and experiment_id.strip():
        return normalize_experiment_id(experiment_id)
    return DEFAULT_EXPERIMENT_SLUG


def k8s_workspace_root(
    sample_dir: Path, *, experiment_id: str | None = None
) -> Path:
    """Root for one experiment: ``sampleN/k8s-experiments/<slug>/``."""
    slug = resolve_k8s_experiment_id(experiment_id)
    return sample_dir / K8S_EXPERIMENTS_DIRNAME / slug


def experiment_root_from_iteration_path(iteration_path: Path) -> Path:
    """
    Experiment root (``.../k8s-experiments/<slug>/``) for an iteration folder.

    Accepts canonical paths under ``iterations/`` as well as deploy-only paths
    such as ``manual/iteration-NNN/`` — any ancestor that contains
    ``iterations/`` is treated as the experiment root.
    """
    path = iteration_path.expanduser().resolve()
    if not path.is_dir():
        path = path.parent
    current = path
    while True:
        if (current / ITERATIONS_DIRNAME).is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise ValueError(
        f"no experiment root (directory containing {ITERATIONS_DIRNAME}/) "
        f"found for {iteration_path}"
    )


def iterations_root(
    sample_dir: Path, *, experiment_id: str | None = None
) -> Path:
    return k8s_workspace_root(sample_dir, experiment_id=experiment_id) / ITERATIONS_DIRNAME


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


def parse_iteration_index(folder_name: str) -> int | None:
    """Extract the 0-based iteration index from a folder name, or ``None``."""
    index, _kind, _failed = parse_iteration_folder_name(folder_name)
    return index


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


def iteration_dir(
    sample_dir: Path,
    iteration_id: str,
    *,
    experiment_id: str | None = None,
) -> Path:
    """Canonical write path before kind suffix: ``…/iterations/iteration-001``."""
    return (
        iterations_root(sample_dir, experiment_id=experiment_id)
        / normalize_iteration_id(iteration_id)
    )


def resolve_iteration_dir(
    sample_dir: Path,
    iteration_id: str,
    *,
    exclude_failed: bool = True,
    experiment_id: str | None = None,
) -> Path:
    """Return the best-matching iteration directory for a logical iteration id."""
    iid = normalize_iteration_id(iteration_id)
    m = re.fullmatch(r"iteration-(\d+)", iid)
    if not m:
        return iteration_dir(
            sample_dir, iteration_id, experiment_id=experiment_id
        )

    phase_slug = m.group(1).zfill(3)
    prefix = f"{ITERATION_PREFIX}{phase_slug}"
    root = iterations_root(sample_dir, experiment_id=experiment_id)
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
    """True if ``path`` looks like an iteration directory that has been written."""
    if not path:
        return False
    candidates = (
        path / "meta.json",
        path / PHASE_SPEC_DIRNAME / "spec.yaml",
        path / PHASE_BENCH_DIRNAME / "config.json",
        path / PHASE_BENCH_DIRNAME / "iteration_feedback.json",
    )
    return any(p.is_file() for p in candidates)


def iteration_meta_path(iteration_path: Path) -> Path:
    return iteration_path / "meta.json"


def iteration_spec_dir(iteration_path: Path) -> Path:
    """Spec phase folder (``03-spec/``)."""
    return iteration_path / PHASE_SPEC_DIRNAME


def iteration_spec_path(iteration_path: Path) -> Path:
    return iteration_spec_dir(iteration_path) / "spec.yaml"


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


def iteration_manifests_dir(iteration_path: Path) -> Path:
    """Rendered K8s manifests live under the spec phase: ``03-spec/manifests/``."""
    return iteration_spec_dir(iteration_path) / "manifests"


def iteration_deploy_dir(iteration_path: Path) -> Path:
    """Deploy phase folder (``04-deploy/``)."""
    return iteration_path / PHASE_DEPLOY_DIRNAME


def deploy_probe_record_path(iteration_path: Path) -> Path:
    return iteration_deploy_dir(iteration_path) / "probe.json"


def deploy_bench_record_path(iteration_path: Path) -> Path:
    return iteration_deploy_dir(iteration_path) / "bench.json"


def iteration_decision_dir(iteration_path: Path) -> Path:
    """Decision phase folder (``01-decision/``)."""
    return iteration_path / PHASE_DECISION_DIRNAME


def iteration_decision_log_path(iteration_path: Path) -> Path:
    """Per-stage log file for the refinement decision (``01-decision/phase.log``)."""
    return iteration_decision_dir(iteration_path) / "phase.log"


def iteration_code_log_path(iteration_path: Path) -> Path:
    """Per-stage log file for code refinement + FT validation (``02-code/phase.log``)."""
    return iteration_code_phase_dir(iteration_path) / "phase.log"


def iteration_spec_log_path(iteration_path: Path) -> Path:
    """Per-stage log file for spec generation (``03-spec/phase.log``)."""
    return iteration_spec_dir(iteration_path) / "phase.log"


def iteration_deploy_log_path(iteration_path: Path) -> Path:
    """Per-stage log file for cluster deploy + readiness probe (``04-deploy/phase.log``)."""
    return iteration_deploy_dir(iteration_path) / "phase.log"


def iteration_log_path(iteration_path: Path) -> Path:
    """Top-level iteration log (one-liner header + final outcome)."""
    return iteration_path / "iteration.log"


def iteration_code_phase_dir(iteration_path: Path) -> Path:
    """
    Code-refinement phase folder (``02-code/``).

    Holds the regenerated code under ``code/`` plus the LLM transcript
    (``prompt.log``, ``response.log``), the ``functional_tests/`` outputs
    from validating that regeneration, and ``failure_report.json`` if the
    tests did not pass.
    """
    return iteration_path / PHASE_CODE_DIRNAME


def iteration_code_snapshot_dir(iteration_path: Path) -> Path:
    """Directory containing the application source for this iteration (``02-code/code/``)."""
    return iteration_code_phase_dir(iteration_path) / "code"


def find_latest_code_dir(
    sample_dir: Path, *, experiment_id: str | None = None
) -> Path | None:
    """
    Newest non-failed iteration ``02-code/code/`` snapshot, or ``None``.

    Excludes ``-failed`` folders so a broken code-refinement attempt does not
    become the copy source for deployment/spec iterations.
    """
    root = iterations_root(sample_dir, experiment_id=experiment_id)
    if not root.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for child in root.iterdir():
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        code_dir = iteration_code_snapshot_dir(child)
        if not code_dir.is_dir() or not any(code_dir.iterdir()):
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        if best is None or idx > best[0]:
            best = (idx, code_dir)
    return best[1] if best is not None else None


def latest_code_dir(
    sample_dir: Path, *, fallback: Path, experiment_id: str | None = None
) -> Path:
    """
    Return the newest non-failed iteration ``code/`` snapshot, else ``fallback``.

    Refined code lives under ``iterations/iteration-NNN-*/02-code/code/``.
    For k8s bench, pass :func:`k8s_fallback_code_dir` as ``fallback`` — not
    sample-level ``code/`` from distributed bench.
    """
    return (
        find_latest_code_dir(sample_dir, experiment_id=experiment_id) or fallback
    )


def k8s_fallback_code_dir(
    sample_dir: Path, *, experiment_id: str | None = None
) -> Path:
    """
    Fallback code directory for k8s bench when no iteration snapshot matches yet.

    K8s experiments always generate baseline code under ``iteration-000*``; we do
    not use sample-level ``code/`` from ``--mode generate``.
    """
    root = iterations_root(sample_dir, experiment_id=experiment_id)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.name.startswith(f"{ITERATION_PREFIX}000"):
                continue
            if iteration_folder_is_failed(child.name):
                continue
            code_dir = iteration_code_snapshot_dir(child)
            if code_dir.is_dir() and any(code_dir.iterdir()):
                return code_dir
    return iteration_code_snapshot_dir(
        resolve_iteration_dir(
            sample_dir,
            iteration_id_for_index(0),
            experiment_id=experiment_id,
        )
    )


def latest_spec_path(
    sample_dir: Path, *, experiment_id: str | None = None
) -> tuple[Path, Path] | None:
    """
    Return ``(spec_yaml_path, iteration_dir)`` for the most recent iteration
    that has a ``spec.yaml`` on disk, or ``None`` if no spec was ever written.

    Walks back through iteration indices ``[max .. 0]`` and considers **both
    successful and ``-failed`` folders**. This is the canonical "current spec"
    handle for code refinement, where the iteration's own spec has not been
    materialized yet (the spec stage runs *after* the code stage). When the
    immediately prior iteration failed before producing bench data, its
    attempted ``spec.yaml`` is still the last source of truth for the
    deployment shape — the LLM should see it.

    Among multiple folders sharing the same index (e.g. both ``iteration-005``
    and ``iteration-005-spec-failed`` exist transiently during renames), the
    non-failed one is preferred.
    """
    root = iterations_root(sample_dir, experiment_id=experiment_id)
    if not root.is_dir():
        return None

    by_index: dict[int, list[Path]] = {}
    for child in root.iterdir():
        if not child.is_dir():
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        by_index.setdefault(idx, []).append(child)

    for idx in sorted(by_index.keys(), reverse=True):
        candidates = by_index[idx]
        non_failed = [c for c in candidates if not iteration_folder_is_failed(c.name)]
        ordered = (non_failed or []) + [
            c for c in candidates if iteration_folder_is_failed(c.name)
        ]
        for cand in ordered:
            spec = find_iteration_spec_path(cand)
            if spec is not None:
                return spec, cand
    return None


def image_id_from_test_log(test_log: Path) -> str | None:
    """Grep the first ``sha256:<64hex>`` from a test/bench log."""
    pattern = re.compile(r"sha256:[0-9a-f]{64}")
    try:
        for line in test_log.read_text(encoding="utf-8").splitlines():
            match = pattern.search(line)
            if match:
                return match.group(0)
    except OSError:
        pass
    return None


def iteration_functional_tests_dir(iteration_path: Path) -> Path:
    """Functional-test outputs from the code-refinement validation step (``02-code/functional_tests/``)."""
    return iteration_code_phase_dir(iteration_path) / "functional_tests"


# Per-attempt subdirectories for LLM-driven phases.
#
# When a phase needs multiple LLM rounds (codegen retry on FT failure, spec
# validation retries, baseline deploy-probe retries) every round is captured
# under ``<phase>/attempts/<NNN>/`` so the failed prompt+response+outcome stay
# auditable instead of being overwritten by the winning attempt. The winning
# attempt's prompt/response are also surfaced at the phase top level (keeping
# the existing contract for ``experiment_summary`` and friends).
ATTEMPTS_DIRNAME = "attempts"
BASELINE_CODEGEN_META_FILENAME = "codegen.json"


def iteration_code_attempts_dir(iteration_path: Path) -> Path:
    """``02-code/attempts/`` — one numbered subdir per codegen attempt."""
    return iteration_code_phase_dir(iteration_path) / ATTEMPTS_DIRNAME


def iteration_spec_attempts_dir(iteration_path: Path) -> Path:
    """``03-spec/attempts/`` — one numbered subdir per spec LLM call / probe round."""
    return iteration_spec_dir(iteration_path) / ATTEMPTS_DIRNAME


def baseline_codegen_meta_path(iteration_path: Path) -> Path:
    """``02-code/codegen.json`` — metadata for the baseline regenerate-mode codegen."""
    return iteration_code_phase_dir(iteration_path) / BASELINE_CODEGEN_META_FILENAME


def _format_attempt_index(n: int) -> str:
    if n < 1:
        raise ValueError(f"attempt index must be >= 1, got {n}")
    return f"{n:03d}"


def attempt_subdir(attempts_dir: Path, attempt_index: int) -> Path:
    """Return ``<attempts_dir>/<NNN>/`` for a 1-based attempt index."""
    return attempts_dir / _format_attempt_index(attempt_index)


def next_attempt_index(attempts_dir: Path) -> int:
    """
    Return the next 1-based index to use under ``attempts_dir``.

    Reads existing ``NNN`` subdirectories (ignoring non-numeric names) and
    returns ``max + 1`` — or ``1`` when the directory is empty / missing. Used
    by codegen + spec stages so callers do not have to track attempt counters
    in memory; the filesystem is the single source of truth.
    """
    if not attempts_dir.is_dir():
        return 1
    best = 0
    for child in attempts_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            n = int(child.name)
        except ValueError:
            continue
        if n > best:
            best = n
    return best + 1


def iteration_bench_dir(iteration_path: Path) -> Path:
    """Bench phase folder (``05-bench/``)."""
    return iteration_path / PHASE_BENCH_DIRNAME


def default_k8s_namespace(
    iteration_id: str, *, experiment_id: str | None = None
) -> str:
    """Kubernetes namespace for an iteration (includes experiment slug)."""
    iid = normalize_iteration_id(iteration_id)
    eid = resolve_k8s_experiment_id(experiment_id)
    return f"baxbench-{eid}-{iid}"


def iteration_id_for_index(iteration_index: int) -> str:
    """0-based iteration index → ``iteration-000`` (baseline), ``iteration-001``, …"""
    if iteration_index < 0:
        raise ValueError("iteration_index must be >= 0")
    return f"{ITERATION_PREFIX}{iteration_index:03d}"


def is_baseline_iteration(iteration_index: int) -> bool:
    return iteration_index == 0


def _max_iteration_number(
    sample_dir: Path, *, experiment_id: str | None = None
) -> int:
    root = iterations_root(sample_dir, experiment_id=experiment_id)
    if not root.is_dir():
        return 0
    max_n = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        idx = parse_iteration_index(child.name)
        if idx is not None:
            max_n = max(max_n, idx)
    return max_n


def new_iteration_id(
    sample_dir: Path, *, experiment_id: str | None = None
) -> str:
    """Return the next ``iteration-NNN`` id."""
    iterations_root(sample_dir, experiment_id=experiment_id).mkdir(
        parents=True, exist_ok=True
    )
    return (
        f"{ITERATION_PREFIX}"
        f"{_max_iteration_number(sample_dir, experiment_id=experiment_id) + 1:03d}"
    )


def list_iteration_dirs(
    sample_dir: Path, *, experiment_id: str | None = None
) -> list[Path]:
    """List successful iteration directories that contain a spec."""
    root = iterations_root(sample_dir, experiment_id=experiment_id)
    if not root.is_dir():
        return []

    found: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        if iteration_folder_is_failed(child.name):
            continue
        iid = iteration_id_for_index(idx)
        if find_iteration_spec_path(child) is not None:
            found[iid] = child

    return [
        p
        for _, p in sorted(
            found.items(),
            key=lambda item: parse_iteration_index(item[1].name) or 0,
        )
    ]


def bench_dir_has_complete_run(bench_dir: Path) -> bool:
    """True when ``05-bench/`` already has a finished Locust run."""
    return (bench_dir / "config.json").is_file() or (
        bench_dir / "iteration_feedback.json"
    ).is_file()


def _bench_run_complete(bench: Path) -> bool:
    return bench_dir_has_complete_run(bench)


def resolve_bench_dir(
    sample_dir: Path,
    iteration_id: str,
    *,
    experiment_id: str | None = None,
) -> Path | None:
    """Return ``iterations/<id>/bench`` when a finished run exists."""
    ip = resolve_iteration_dir(
        sample_dir, iteration_id, experiment_id=experiment_id
    )
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
    experiment_id: str | None = None,
) -> Path:
    del load_profile, timestamp
    ip = resolve_iteration_dir(
        sample_dir, iteration_id, experiment_id=experiment_id
    )
    from .layout import ensure_iteration_core_layout

    ensure_iteration_core_layout(ip)
    return iteration_bench_dir(ip)

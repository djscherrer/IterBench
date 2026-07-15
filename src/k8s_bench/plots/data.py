from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..workspace import (
    ITERATIONS_DIRNAME,
    bench_dir_has_complete_run,
    iteration_bench_dir,
    iteration_folder_is_failed,
    parse_iteration_folder_name,
    parse_iteration_index,
)

RefinementKind = Literal["baseline", "code", "spec"]

_GOODPUT_HISTORY_ENTRY_RE = re.compile(r"(\d+)u:([\d.]+)/s")
_ADAPTIVE_V2_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) "
    r"final_users=(?P<final_users>\S+) "
    r"low_ok=(?P<low_ok>\S+) "
    r"high_bad=(?P<high_bad>\S+) "
    r"goodput_history=\[(?P<history>[^\]]*)\]"
)


@dataclass(frozen=True)
class IterationGoodputPoint:
    iteration_index: int
    iteration_id: str
    folder_name: str
    refinement_kind: RefinementKind
    goodput_rps: float
    users_at_peak: int | None
    final_users: int | None
    goodput_history: list[tuple[int, float]]


def resolve_experiment_root(path: Path) -> Path:
    """
    Accept an experiment root (contains ``iterations/``) or a sample directory.

    When ``path`` is a sample directory, callers must set
    ``BAXBENCH_K8S_EXPERIMENT`` before invoking (same as other k8s tools).
    """
    path = path.expanduser().resolve()
    if (path / ITERATIONS_DIRNAME).is_dir():
        return path
    from ..workspace import k8s_workspace_root

    candidate = k8s_workspace_root(path)
    if (candidate / ITERATIONS_DIRNAME).is_dir():
        return candidate
    raise FileNotFoundError(
        f"No k8s experiment iterations/ under {path} "
        f"(also tried {candidate})"
    )


def _read_bench_log(bench_dir: Path) -> str:
    bench_log = bench_dir / "bench.log"
    if bench_log.is_file():
        return bench_log.read_text(encoding="utf-8", errors="replace")
    feedback = bench_dir / "iteration_feedback.json"
    if feedback.is_file():
        payload = json.loads(feedback.read_text(encoding="utf-8"))
        summary = payload.get("load_run_summary") or ""
        if isinstance(summary, str) and summary:
            return summary
    return ""


def _parse_goodput_history(text: str) -> list[tuple[int, float]]:
    entries: list[tuple[int, float]] = []
    for m in _GOODPUT_HISTORY_ENTRY_RE.finditer(text):
        entries.append((int(m.group(1)), float(m.group(2))))
    return entries


def _parse_adaptive_v2_stop(text: str) -> dict[str, str] | None:
    last: dict[str, str] | None = None
    for line in text.splitlines():
        m = _ADAPTIVE_V2_STOP_RE.search(line)
        if not m:
            continue
        last = {
            "reason": m.group("reason"),
            "final_users": m.group("final_users"),
            "history": m.group("history").strip(),
        }
    return last


def _refinement_kind_from_folder(folder_name: str) -> RefinementKind:
    _idx, kind, _failed = parse_iteration_folder_name(folder_name)
    if kind == "code":
        return "code"
    if kind == "spec":
        return "spec"
    return "baseline"


def _refinement_kind_from_meta(meta_path: Path, folder_name: str) -> RefinementKind:
    if not meta_path.is_file():
        return _refinement_kind_from_folder(folder_name)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _refinement_kind_from_folder(folder_name)
    action = str(meta.get("refinement_action") or "").strip().lower()
    if action == "code":
        return "code"
    if action == "deployment":
        return "spec"
    if action == "baseline":
        return "baseline"
    return _refinement_kind_from_folder(folder_name)


def collect_iteration_goodput_points(experiment_root: Path) -> list[IterationGoodputPoint]:
    iterations_dir = experiment_root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return []

    by_index: dict[int, Path] = {}
    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        bench_dir = iteration_bench_dir(child)
        if not bench_dir_has_complete_run(bench_dir):
            continue
        existing = by_index.get(idx)
        if existing is None or child.stat().st_mtime >= existing.stat().st_mtime:
            by_index[idx] = child

    points: list[IterationGoodputPoint] = []
    for idx in sorted(by_index):
        iteration_path = by_index[idx]
        bench_dir = iteration_bench_dir(iteration_path)
        bench_text = _read_bench_log(bench_dir)
        stop = _parse_adaptive_v2_stop(bench_text)
        history = (
            _parse_goodput_history(stop["history"])
            if stop and stop.get("history")
            else []
        )
        from .ramp_data import (
            is_explore_refine_bench,
            peak_goodput_from_bench_log,
            sustained_goodput_from_bench,
        )

        sustained = sustained_goodput_from_bench(bench_dir, log_text=bench_text)
        if sustained is not None and sustained.goodput_rps > 0:
            goodput_rps = sustained.goodput_rps
            users_at_peak = sustained.users
        elif is_explore_refine_bench(bench_dir):
            goodput_rps = 0.0
            users_at_peak = None
        else:
            goodput_rps, users_at_peak = peak_goodput_from_bench_log(
                bench_text,
                history,
            )

        final_users: int | None = None
        if stop and stop.get("final_users"):
            try:
                final_users = int(stop["final_users"])
            except ValueError:
                final_users = None

        kind = _refinement_kind_from_meta(
            iteration_path / "meta.json",
            iteration_path.name,
        )
        points.append(
            IterationGoodputPoint(
                iteration_index=idx,
                iteration_id=f"iteration-{idx:03d}",
                folder_name=iteration_path.name,
                refinement_kind=kind,
                goodput_rps=goodput_rps,
                users_at_peak=users_at_peak,
                final_users=final_users,
                goodput_history=history,
            )
        )
    return points

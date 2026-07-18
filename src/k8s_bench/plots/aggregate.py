"""
Cross-experiment aggregation for the thesis evaluation chapter.

Unlike :mod:`.data` / :mod:`.goodput_trajectory` (one k8s experiment at a
time), this module walks the whole ``results/`` tree — every
``<model>/<scenario>/<env>/<variant>/sampleN/k8s-experiments/<slug>/`` cell —
and builds tidy tables (as :class:`pandas.DataFrame`) for cross-cutting
comparisons: model vs. model, framework vs. framework, code vs. deployment
refinement, and failure taxonomy.

Entry point: :func:`collect_all`. See ``scripts/analysis/aggregate_evaluation.py``
for the CLI that turns this into CSVs and figures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ..workspace.paths import (
    ITERATIONS_DIRNAME,
    PHASE_SPEC_DIRNAME,
    find_iteration_spec_path,
    iteration_bench_dir,
    iteration_folder_is_failed,
    parse_iteration_folder_name,
    parse_iteration_index,
)
from .data import IterationGoodputPoint, collect_iteration_goodput_points

_SAMPLE_RE = re.compile(r"^sample(\d+)$")


@dataclass(frozen=True)
class CellKey:
    model: str
    scenario: str
    env: str
    variant: str
    sample: int


def _cell_key_from_exp_dir(exp_dir: Path, results_root: Path) -> CellKey | None:
    """``exp_dir`` is .../sampleN/k8s-experiments/<slug>``."""
    try:
        rel = exp_dir.relative_to(results_root)
    except ValueError:
        return None
    parts = rel.parts
    # model/scenario/env/variant/sampleN/k8s-experiments/<slug>
    if len(parts) < 7 or parts[-2] != "k8s-experiments":
        return None
    model, scenario, env, variant, sample_part = parts[:5]
    m = _SAMPLE_RE.match(sample_part)
    if not m:
        return None
    return CellKey(
        model=model,
        scenario=scenario,
        env=env,
        variant=variant,
        sample=int(m.group(1)),
    )


def discover_cells(
    results_root: Path,
    *,
    experiment_slug: str = "results",
    include_models: set[str] | None = None,
    exclude_models: set[str] | None = None,
) -> list[tuple[CellKey, Path]]:
    """Find every ``k8s-experiments/<experiment_slug>`` dir with an ``iterations/`` tree."""
    cells: list[tuple[CellKey, Path]] = []
    for sample_dir in sorted(results_root.glob("*/*/*/*/sample*")):
        if not sample_dir.is_dir():
            continue
        exp_dir = sample_dir / "k8s-experiments" / experiment_slug
        if not (exp_dir / ITERATIONS_DIRNAME).is_dir():
            continue
        key = _cell_key_from_exp_dir(exp_dir, results_root)
        if key is None:
            continue
        if include_models is not None and key.model not in include_models:
            continue
        if exclude_models is not None and key.model in exclude_models:
            continue
        cells.append((key, exp_dir))
    return cells


# ---------------------------------------------------------------------------
# Per-cell summary (RQ1 / RQ3 / RQ4)
# ---------------------------------------------------------------------------


def _load_total_llm_cost(exp_dir: Path) -> float | None:
    ledger = exp_dir / "llm_cost_ledger.json"
    if not ledger.is_file():
        return None
    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    by_iteration = payload.get("by_iteration")
    if isinstance(by_iteration, dict) and by_iteration:
        try:
            return float(sum(by_iteration.values()))
        except (TypeError, ValueError):
            pass
    by_call_type = payload.get("by_call_type")
    if isinstance(by_call_type, dict) and by_call_type:
        try:
            return float(sum(by_call_type.values()))
        except (TypeError, ValueError):
            pass
    return None


def cell_summary_row(key: CellKey, exp_dir: Path) -> dict[str, Any]:
    iterations_dir = exp_dir / ITERATIONS_DIRNAME
    all_dirs = [d for d in iterations_dir.iterdir() if d.is_dir()] if iterations_dir.is_dir() else []
    n_dirs = len(all_dirs)
    n_failed = sum(1 for d in all_dirs if iteration_folder_is_failed(d.name))

    points = collect_iteration_goodput_points(exp_dir)
    baseline_goodput = points[0].goodput_rps if points else None
    final_goodput = points[-1].goodput_rps if points else None
    max_goodput = max((p.goodput_rps for p in points), default=None)
    code_steps = sum(1 for p in points if p.refinement_kind == "code")
    spec_steps = sum(1 for p in points if p.refinement_kind == "spec")

    reached_baseline = any(p.iteration_index == 0 for p in points)
    max_folder_index = max((parse_iteration_index(d.name) or -1) for d in all_dirs) if all_dirs else -1

    return {
        "model": key.model,
        "scenario": key.scenario,
        "env": key.env,
        "variant": key.variant,
        "sample": key.sample,
        "n_iteration_dirs": n_dirs,
        "n_failed_dirs": n_failed,
        "n_complete_points": len(points),
        "max_folder_index": max_folder_index,
        "reached_baseline": reached_baseline,
        "baseline_goodput_rps": baseline_goodput,
        "final_goodput_rps": final_goodput,
        "max_goodput_rps": max_goodput,
        "code_refinement_steps": code_steps,
        "spec_refinement_steps": spec_steps,
        "improvement_ratio": (
            (max_goodput / baseline_goodput)
            if baseline_goodput and baseline_goodput > 0 and max_goodput is not None
            else None
        ),
        "recovered_from_dead_baseline": bool(
            baseline_goodput is not None
            and baseline_goodput == 0.0
            and max_goodput is not None
            and max_goodput > 0.0
        ),
        "total_llm_cost_usd": _load_total_llm_cost(exp_dir),
    }


# ---------------------------------------------------------------------------
# Iteration-level long table: goodput trajectories + spec knobs (RQ1 / RQ5)
# ---------------------------------------------------------------------------

_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)m$")


def _cpu_to_millicores(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    m = _CPU_RE.match(s)
    if m:
        return float(m.group(1))
    try:
        return float(s) * 1000.0
    except ValueError:
        return None


_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)([EPTGMK]i?)?$")
_MEM_UNIT_TO_MI = {
    "Ki": 1 / 1024,
    "Mi": 1.0,
    "Gi": 1024.0,
    "Ti": 1024.0 * 1024,
    "K": 1 / 1024,
    "M": 1.0,
    "G": 1024.0,
    "T": 1024.0 * 1024,
}


def _mem_to_mi(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    m = _MEM_RE.match(s)
    if not m:
        return None
    qty = float(m.group(1))
    unit = m.group(2) or "Mi"
    factor = _MEM_UNIT_TO_MI.get(unit)
    if factor is None:
        return None
    return qty * factor


def _spec_knobs_for_iteration(iteration_path: Path) -> dict[str, Any]:
    spec_path = find_iteration_spec_path(iteration_path) or (
        iteration_path / PHASE_SPEC_DIRNAME / "spec.yaml"
    )
    if not spec_path.is_file():
        return {}
    try:
        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    backend = payload.get("backend") or {}
    backend_resources = backend.get("resources") or {}
    database = payload.get("database") or {}
    return {
        "backend_replicas": backend.get("replicas"),
        "backend_cpu_request_m": _cpu_to_millicores(backend_resources.get("cpu_request")),
        "backend_memory_request_mi": _mem_to_mi(backend_resources.get("memory_request")),
        "database_replicas": database.get("replicas"),
        "database_max_connections": database.get("max_connections"),
    }


def iteration_rows_for_cell(
    key: CellKey, exp_dir: Path, points: list[IterationGoodputPoint]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prev_goodput: float | None = None
    for point in points:
        iteration_path = exp_dir / ITERATIONS_DIRNAME / point.folder_name
        knobs = _spec_knobs_for_iteration(iteration_path)
        delta = None
        delta_pct = None
        if prev_goodput is not None:
            delta = point.goodput_rps - prev_goodput
            if prev_goodput > 0:
                delta_pct = 100.0 * delta / prev_goodput
        rows.append(
            {
                "model": key.model,
                "scenario": key.scenario,
                "env": key.env,
                "sample": key.sample,
                "iteration_index": point.iteration_index,
                "iteration_id": point.iteration_id,
                "refinement_kind": point.refinement_kind,
                "goodput_rps": point.goodput_rps,
                "delta_goodput_rps": delta,
                "delta_goodput_pct": delta_pct,
                "recovered_from_zero": bool(prev_goodput == 0.0 and point.goodput_rps > 0.0),
                **knobs,
            }
        )
        prev_goodput = point.goodput_rps
    return rows


# ---------------------------------------------------------------------------
# Failure taxonomy (RQ6)
# ---------------------------------------------------------------------------


def failure_rows_for_cell(key: CellKey, exp_dir: Path) -> list[dict[str, Any]]:
    iterations_dir = exp_dir / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    phase_dirs = {"01-decision", "02-code", "03-spec", "04-deploy", "05-bench"}
    for iteration_dir in sorted(iterations_dir.iterdir()):
        if not iteration_dir.is_dir():
            continue
        idx, kind, failed = parse_iteration_folder_name(iteration_dir.name)
        for phase_dir in iteration_dir.iterdir():
            if not phase_dir.is_dir() or phase_dir.name not in phase_dirs:
                continue
            failure_path = phase_dir / "failure.json"
            if not failure_path.is_file():
                continue
            try:
                payload = json.loads(failure_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            terminal = payload.get("terminal") or payload
            fkind = terminal.get("kind") or payload.get("kind") or "unknown"
            rows.append(
                {
                    "model": key.model,
                    "scenario": key.scenario,
                    "env": key.env,
                    "iteration_folder": iteration_dir.name,
                    "iteration_index": idx,
                    "refinement_kind": kind or "baseline",
                    "phase": phase_dir.name,
                    "failure_kind": str(fkind),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Top-level aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregateData:
    cells: pd.DataFrame
    iterations: pd.DataFrame
    failures: pd.DataFrame


def collect_all(
    results_root: Path,
    *,
    experiment_slug: str = "results",
    include_models: set[str] | None = None,
    exclude_models: set[str] | None = None,
) -> AggregateData:
    cell_rows: list[dict[str, Any]] = []
    iteration_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    for key, exp_dir in discover_cells(
        results_root,
        experiment_slug=experiment_slug,
        include_models=include_models,
        exclude_models=exclude_models,
    ):
        cell_rows.append(cell_summary_row(key, exp_dir))
        points = collect_iteration_goodput_points(exp_dir)
        iteration_rows.extend(iteration_rows_for_cell(key, exp_dir, points))
        failure_rows.extend(failure_rows_for_cell(key, exp_dir))

    cells = pd.DataFrame(cell_rows)
    iterations = pd.DataFrame(iteration_rows)
    failures = pd.DataFrame(failure_rows)
    return AggregateData(cells=cells, iterations=iterations, failures=failures)

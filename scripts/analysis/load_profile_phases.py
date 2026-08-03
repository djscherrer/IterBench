#!/usr/bin/env python3
"""
Load-profile phase/outcome extraction for the "load profile evaluation" RQ.

Walks the same ``results/<model>/<scenario>/<env>/<variant>/sampleN/
k8s-experiments/<slug>/iterations/`` tree as ``aggregate_evaluation.py``, but
parses each iteration's ``05-bench/bench.log`` directly for the
Explore-and-Refine controller's own phase-transition log lines
(``src/load_bench/load_profiles/shapes/explore_refine.py``) rather than just
its final reported goodput. This recovers information that
``results_aggregate/{cells,iterations}.csv`` does not carry: per-phase
durations (warm-up/explore/recovery/refine), the explore-phase peak goodput,
and the phase-outcome (warm-up survival, recovery success, refine success) at
which a run terminated.

Every quantity here is derived from log text already on disk; no new bench
runs are required.

Usage:
    pipenv run python scripts/analysis/load_profile_phases.py \\
        --exclude-models deepseek-deepseek-v3.2
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from workspace.paths import (  # noqa: E402
    ITERATIONS_DIRNAME,
    iteration_bench_dir,
    iteration_folder_is_failed,
    parse_iteration_index,
)
from plots.aggregate.tables import CellKey, discover_cells  # noqa: E402

# ---------------------------------------------------------------------------
# bench.log parsing
# ---------------------------------------------------------------------------

_ANY_T_RE = re.compile(r"\bt=(\d+(?:\.\d+)?)s\b")
_WARMUP_END_RE = re.compile(
    r"explore-refine warmup end t=(?P<t>\d+(?:\.\d+)?)s at users=\S+ "
    r"healthy=(?P<healthy>True|False) \((?P<reason>[^)]*)\)"
)
_REFINE_START_RE = re.compile(
    r"explore-refine: refine start at users=\S+ max_step=\S+ "
    r"\([\d.]+% of entry\) min_step=\S+ explore_peak=(?P<peak>[\d.]+)/s"
)
_EXPLORE_RAMP_PEAK_RE = re.compile(
    r"explore ramp t=(?P<t>\d+(?:\.\d+)?)s .*? peak=(?P<peak>[\d.]+)/s"
)
_EXPLORE_STOP_PEAK_RE = re.compile(r"peak=\d+u/(?P<peak>[\d.]+)/s")


def _explore_stop_category(action: str | None) -> str | None:
    """Map an explore-stop action string to one of the four stop conditions
    ``ExploreRefineShape._explore_stop_reason``/``_tick_explore`` can raise."""
    if not action:
        return None
    reason = action.removeprefix("explore end (").split(")", 1)[0]
    if reason.startswith("fail%="):
        return "failure-rate"
    if reason.startswith("p95="):
        return "p95-latency"
    if reason.startswith("goodput="):
        return "goodput-collapse"
    if reason.startswith("max users="):
        return "max-users-ceiling"
    return "other"
_PHASE_END_RE = re.compile(
    r"adaptive phase end t=(?P<t>\d+(?:\.\d+)?)s: (?P<action>.+?) \|"
)
_FINAL_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) final_users=(?P<final_users>\S+)"
    r".*?(?: refine_peak=(?P<refine_peak>[\d.]+)/s@(?P<refine_peak_users>\d+)u)?$",
    re.MULTILINE,
)


def _read_bench_log(bench_dir: Path) -> str:
    bench_log = bench_dir / "bench.log"
    if bench_log.is_file():
        return bench_log.read_text(encoding="utf-8", errors="replace")
    feedback = bench_dir / "iteration_feedback.json"
    if feedback.is_file():
        try:
            payload = json.loads(feedback.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        summary = payload.get("load_run_summary") or ""
        if isinstance(summary, str):
            return summary
    return ""


@dataclass(frozen=True)
class PhaseRow:
    warmup_healthy: bool | None
    warmup_duration_s: float | None
    explore_duration_s: float | None
    explore_peak_goodput_rps: float | None
    explore_stop_reason: str | None
    explore_stop_category: str | None
    recovery_attempted: bool
    recovery_duration_s: float | None
    recovery_success: bool | None
    refine_attempted: bool
    refine_duration_s: float | None
    refine_success: bool | None
    sustained_goodput_rps: float | None
    final_stop_reason: str | None
    total_duration_s: float | None


def parse_bench_log(text: str) -> PhaseRow | None:
    """Reconstruct phase boundaries/outcomes from one bench.log's text.

    Returns ``None`` when the log carries no Explore-and-Refine controller
    output at all (e.g. a failed iteration with no bench run).
    """
    if not text or "explore-refine" not in text and "adaptive phase end" not in text:
        return None

    warmup_m = _WARMUP_END_RE.search(text)
    warmup_healthy: bool | None = None
    warmup_duration_s: float | None = None
    if warmup_m:
        warmup_duration_s = float(warmup_m.group("t"))
        warmup_healthy = warmup_m.group("healthy") == "True"

    all_t = [float(m.group(1)) for m in _ANY_T_RE.finditer(text)]
    total_duration_s = max(all_t) if all_t else None

    if warmup_healthy is False:
        return PhaseRow(
            warmup_healthy=False,
            warmup_duration_s=warmup_duration_s,
            explore_duration_s=None,
            explore_peak_goodput_rps=None,
            explore_stop_reason=None,
            explore_stop_category=None,
            recovery_attempted=False,
            recovery_duration_s=None,
            recovery_success=None,
            refine_attempted=False,
            refine_duration_s=None,
            refine_success=None,
            sustained_goodput_rps=0.0,
            final_stop_reason="warmup-unhealthy",
            total_duration_s=total_duration_s if total_duration_s is not None else warmup_duration_s,
        )

    phase_transitions = [
        (float(m.group("t")), m.group("action")) for m in _PHASE_END_RE.finditer(text)
    ]

    explore_stop_t: float | None = None
    explore_stop_reason: str | None = None
    recovery_success_t: float | None = None
    recovery_exhausted_t: float | None = None
    refine_stop_t: float | None = None
    refine_stop_reason: str | None = None
    for t, action in phase_transitions:
        if action.startswith("recovery healthy -> "):
            if recovery_success_t is None:
                recovery_success_t = t
        elif action.startswith("recovery unhealthy -> recovery gave up"):
            recovery_exhausted_t = t
        elif action.startswith("recovery unhealthy -> recovery retry"):
            continue
        elif recovery_success_t is not None:
            # A refine-phase tick. Track the last one that is itself a
            # terminal decision (stall/overload/ceiling); intermediate
            # "refine ramp"/"refine wait" ticks are not phase boundaries.
            if (
                action.startswith("refine stall")
                or action.startswith("refine overload")
                or action.startswith("refine max users ceiling")
            ):
                refine_stop_t = t
                refine_stop_reason = action
        elif explore_stop_t is None:
            explore_stop_t = t
            explore_stop_reason = action

    # Primary source: the peak embedded in the explore-stop transition text
    # itself (``_begin_recovery``'s return string always carries
    # ``peak=<users>u/<goodput>/s`` at the exact moment explore ends, even
    # when explore stopped on its very first decision with no ramp bump
    # logged yet). Falls back to the refine-start line's ``explore_peak=``
    # field (same underlying value, logged later) and finally to the max
    # over "explore ramp ... peak=" lines, for the rare case neither exists.
    explore_peak_goodput_rps: float | None = None
    if explore_stop_reason:
        stop_peak_m = _EXPLORE_STOP_PEAK_RE.search(explore_stop_reason)
        if stop_peak_m:
            explore_peak_goodput_rps = float(stop_peak_m.group("peak"))
    if explore_peak_goodput_rps is None:
        refine_start_m = _REFINE_START_RE.search(text)
        if refine_start_m:
            explore_peak_goodput_rps = float(refine_start_m.group("peak"))
    if explore_peak_goodput_rps is None:
        ramp_peaks = [float(m.group("peak")) for m in _EXPLORE_RAMP_PEAK_RE.finditer(text)]
        if ramp_peaks:
            explore_peak_goodput_rps = max(ramp_peaks)

    final_m = None
    for final_m in _FINAL_STOP_RE.finditer(text):
        pass  # last match wins
    final_stop_reason = final_m.group("reason") if final_m else None
    sustained_goodput_rps: float | None = None
    if final_m and final_m.group("refine_peak"):
        sustained_goodput_rps = float(final_m.group("refine_peak"))
    elif recovery_success_t is not None and refine_stop_t is None:
        # Reached refine but no terminal "refine stall/overload/ceiling"
        # line matched (e.g. run-time-elapsed cutoff mid-refine); treat as
        # refine attempted without a confirmed stable estimate.
        sustained_goodput_rps = 0.0
    elif final_stop_reason == "recovery-unhealthy":
        sustained_goodput_rps = 0.0

    explore_duration_s = (
        (explore_stop_t - warmup_duration_s)
        if explore_stop_t is not None and warmup_duration_s is not None
        else None
    )

    recovery_attempted = explore_stop_t is not None
    recovery_success = None
    recovery_duration_s = None
    if recovery_attempted:
        recovery_end_t = recovery_success_t if recovery_success_t is not None else recovery_exhausted_t
        recovery_success = recovery_success_t is not None
        if recovery_end_t is not None and explore_stop_t is not None:
            recovery_duration_s = recovery_end_t - explore_stop_t

    refine_attempted = recovery_success_t is not None
    refine_success = None
    refine_duration_s = None
    if refine_attempted:
        refine_success = bool(sustained_goodput_rps and sustained_goodput_rps > 0)
        refine_end_t = refine_stop_t if refine_stop_t is not None else total_duration_s
        if refine_end_t is not None:
            refine_duration_s = refine_end_t - recovery_success_t

    return PhaseRow(
        warmup_healthy=warmup_healthy,
        warmup_duration_s=warmup_duration_s,
        explore_duration_s=explore_duration_s,
        explore_peak_goodput_rps=explore_peak_goodput_rps,
        explore_stop_reason=explore_stop_reason,
        explore_stop_category=_explore_stop_category(explore_stop_reason),
        recovery_attempted=recovery_attempted,
        recovery_duration_s=recovery_duration_s,
        recovery_success=recovery_success,
        refine_attempted=refine_attempted,
        refine_duration_s=refine_duration_s,
        refine_success=refine_success,
        sustained_goodput_rps=sustained_goodput_rps,
        final_stop_reason=final_stop_reason if final_stop_reason else refine_stop_reason,
        total_duration_s=total_duration_s,
    )


# ---------------------------------------------------------------------------
# Directory walk
# ---------------------------------------------------------------------------


def rows_for_cell(key: CellKey, exp_dir: Path) -> list[dict[str, Any]]:
    iterations_dir = exp_dir / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        bench_dir = iteration_bench_dir(child)
        text = _read_bench_log(bench_dir)
        parsed = parse_bench_log(text)
        if parsed is None:
            continue
        row = {
            "model": key.model,
            "scenario": key.scenario,
            "env": key.env,
            "sample": key.sample,
            "iteration_index": idx,
            "iteration_id": child.name,
            **vars(parsed),
        }
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results-root", type=Path, default=_REPO_ROOT / "results")
    ap.add_argument("--experiment-slug", default="results")
    ap.add_argument(
        "--out-csv", type=Path, default=_REPO_ROOT / "results_aggregate" / "load_profile_phases.csv"
    )
    ap.add_argument("--include-models", nargs="*", default=None)
    ap.add_argument("--exclude-models", nargs="*", default=None)
    args = ap.parse_args()

    if not args.results_root.is_dir():
        print(f"results root not found: {args.results_root}", file=sys.stderr)
        return 2

    all_rows: list[dict[str, Any]] = []
    for key, exp_dir in discover_cells(
        args.results_root,
        experiment_slug=args.experiment_slug,
        include_models=set(args.include_models) if args.include_models else None,
        exclude_models=set(args.exclude_models) if args.exclude_models else None,
    ):
        all_rows.extend(rows_for_cell(key, exp_dir))

    if not all_rows:
        print("No parseable bench.log files found.", file=sys.stderr)
        return 1

    df = pd.DataFrame(all_rows)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out_csv, index=False)
    print(f"Wrote {args.out_csv} ({len(df)} rows)")

    n = len(df)
    print("\n== Phase-outcome funnel (all bench runs) ==")
    print(f"total bench runs parsed:        {n}")
    print(f"survived warm-up:                {int((df['warmup_healthy'] == True).sum())} "  # noqa: E712
          f"({100 * (df['warmup_healthy'] == True).mean():.1f}%)")  # noqa: E712
    entered_recovery = df["recovery_attempted"] == True  # noqa: E712
    print(f"triggered explore-stop:          {int(entered_recovery.sum())} "
          f"({100 * entered_recovery.mean():.1f}%)")
    among_recovery = df.loc[entered_recovery]
    if len(among_recovery):
        print(f"  recovery succeeded:            {int((among_recovery['recovery_success'] == True).sum())} "  # noqa: E712
              f"({100 * (among_recovery['recovery_success'] == True).mean():.1f}% of those entering recovery)")  # noqa: E712
    entered_refine = df["refine_attempted"] == True  # noqa: E712
    print(f"reached refine phase:            {int(entered_refine.sum())} "
          f"({100 * entered_refine.mean():.1f}%)")
    among_refine = df.loc[entered_refine]
    if len(among_refine):
        print(f"  refine produced an estimate:   {int((among_refine['refine_success'] == True).sum())} "  # noqa: E712
              f"({100 * (among_refine['refine_success'] == True).mean():.1f}% of those reaching refine)")  # noqa: E712

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

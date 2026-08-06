#!/usr/bin/env python3
"""
Compare repeated load-profile measurements for the same fixed candidates.

The original experiment tree and a deploy-only re-verification tree have the
same candidate layout.  This command parses both trees at iteration level and
joins rows on model, scenario, environment, variant, sample, and iteration
number.  It writes:

``pairs.csv``
    One row per candidate that is present in either tree.  Paired rows carry
    signed/absolute sustained-goodput deviations and the explore-peak versus
    sustained-goodput gap for each run.
``summary.csv``
    Overall, per-model, per-network-evidence, per-artifact-status, and
    per-model/environment repeatability statistics.

The pair table also carries the persisted NodePort target, the Locust master
addresses found in worker logs, the first bench timestamp, and a SHA-256 of
``bench.log``.  ``artifact_comparison=unchanged`` means that the reverified
tree contains the same bench log byte-for-byte; it is useful for detecting a
partial archive that copied rows without actually re-running them.
The ``historical_network_phase`` field uses the local ``bench.log`` timestamp
and the two repository cutovers (15 July 06:52 for the NodePort resolver and
17 July 02:00 for Kubernetes/Flannel pinning) as reproducible cohort labels.

The sustained-goodput variance is the ordinary sample variance (``ddof=1``)
of the paired run values in each group.  Relative deviation is measured
against the original run, while the symmetric percent difference is also
reported for pairs where both runs are positive.  The peak/sustained gap uses
the RQ2 convention ``100 * (explore_peak - sustained) / sustained``; a
negative value means refine settled above the explore peak.

Typical use after fetching and unpacking the re-verification archive:

    .venv/bin/python scripts/analysis/load_profile_repeatability.py \
        --original-root results \
        --reverified-root results_reverified \
        --out-dir results_aggregate/repeatability

The command is intentionally safe to rerun while the remote job is still
producing data: unmatched rows are retained and counted, so partial archives
do not silently look complete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_ANALYSIS = Path(__file__).resolve().parent
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

from plots.aggregate.tables import discover_cells  # noqa: E402
from load_profile_phases import rows_for_cell  # noqa: E402


JOIN_COLUMNS = [
    "model",
    "scenario",
    "env",
    "variant",
    "sample",
    "iteration_index",
]
PHASE_COLUMNS = [
    "iteration_id",
    "warmup_healthy",
    "warmup_duration_s",
    "explore_duration_s",
    "explore_peak_goodput_rps",
    "explore_stop_reason",
    "explore_stop_category",
    "recovery_attempted",
    "recovery_duration_s",
    "recovery_success",
    "refine_attempted",
    "refine_duration_s",
    "refine_success",
    "sustained_goodput_rps",
    "final_stop_reason",
    "total_duration_s",
]

# These fields are deliberately evidence fields rather than a claim that the
# packet path can be reconstructed perfectly after the fact.  The bench
# configuration records the NodePort target, while the distributed Locust
# worker logs record the address used to contact the master.  Together they
# distinguish the historical control-network runs from the experiment-LAN
# runs in the archived trees.
NETWORK_COLUMNS = [
    "bench_start_time",
    "historical_network_phase",
    "nodeport_target",
    "nodeport_target_network",
    "locust_master_ips",
    "locust_master_network",
    "bench_log_sha256",
]
_LOG_TIMESTAMP_RE = re.compile(
    r"(?:INFO|WARNING)\s+(?P<timestamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)"
)
_MASTER_IP_RE = re.compile(
    r"master\s+(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):\d+"
)
_IP_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
_LOCUST_LAN_CUTOFF = datetime(2026, 7, 15, 6, 52, 14)
_K8S_LAN_CUTOFF = datetime(2026, 7, 17, 2, 0, 56)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _network_class_for_target(target: object) -> str:
    """Classify the persisted NodePort target without resolving DNS."""
    if target is None or (isinstance(target, float) and math.isnan(target)):
        return "missing"
    value = str(target)
    match = _IP_RE.search(value)
    if match:
        ip = match.group(0)
        return "internal_ip" if ip.startswith("10.") else "control_or_other_ip"
    host = value.split("//", 1)[-1].split(":", 1)[0]
    return "short_hostname" if host and "." not in host else "other_hostname"


def _master_network_class(ips: list[str]) -> str:
    if not ips:
        return "no_evidence"
    if all(ip.startswith("10.") for ip in ips):
        return "internal"
    if all(ip.startswith("155.") for ip in ips):
        return "control"
    return "mixed_or_other"


def _historical_network_phase(timestamp: str | None) -> str:
    """Classify the two network-migration windows using local bench.log time."""
    if not timestamp:
        return "unknown"
    try:
        value = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return "unknown"
    if value < _LOCUST_LAN_CUTOFF:
        return "pre_locust_target_fix"
    if value < _K8S_LAN_CUTOFF:
        return "locust_fixed_k8s_unpinned"
    return "post_k8s_network_fix"


def _iteration_network_metadata(exp_dir: Path, iteration_id: str) -> dict[str, object]:
    """Read network evidence and an artifact identity for one bench run."""
    bench_dir = exp_dir / "iterations" / iteration_id / "05-bench"
    bench_log = bench_dir / "bench.log"
    try:
        text = bench_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""

    timestamp_match = _LOG_TIMESTAMP_RE.search(text)
    timestamp = timestamp_match.group("timestamp") if timestamp_match else None
    master_ips: set[str] = set()
    for path in sorted((bench_dir / "locust" / "logs").glob("worker-*.log")):
        try:
            worker_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        master_ips.update(_MASTER_IP_RE.findall(worker_text))
    master_ip_list = sorted(master_ips)

    target: object = None
    config_path = bench_dir / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        deploy_result = payload.get("deploy_result") or {}
        target = deploy_result.get("nodeport_target") or payload.get("nodeport_target")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    return {
        "bench_start_time": timestamp,
        "historical_network_phase": _historical_network_phase(timestamp),
        "nodeport_target": target,
        "nodeport_target_network": _network_class_for_target(target),
        "locust_master_ips": ";".join(master_ip_list) if master_ip_list else None,
        "locust_master_network": _master_network_class(master_ip_list),
        "bench_log_sha256": _sha256(bench_log),
    }


def collect_runs(
    results_root: Path,
    *,
    experiment_slug: str,
    include_models: set[str] | None = None,
    exclude_models: set[str] | None = None,
) -> pd.DataFrame:
    """Parse all phase rows and attach the full candidate identity."""
    rows: list[dict[str, object]] = []
    for key, exp_dir in discover_cells(
        results_root,
        experiment_slug=experiment_slug,
        include_models=include_models,
        exclude_models=exclude_models,
    ):
        for row in rows_for_cell(key, exp_dir):
            metadata = _iteration_network_metadata(exp_dir, str(row["iteration_id"]))
            rows.append(
                {
                    "model": key.model,
                    "scenario": key.scenario,
                    "env": key.env,
                    "variant": key.variant,
                    "sample": key.sample,
                    **row,
                    **metadata,
                }
            )
    if not rows:
        return pd.DataFrame(columns=[*JOIN_COLUMNS, *PHASE_COLUMNS, *NETWORK_COLUMNS])

    frame = pd.DataFrame(rows)
    # A malformed or hand-copied tree can contain two suffix variants for one
    # logical iteration.  Prefer the canonical path deterministically rather
    # than producing a many-to-many merge that inflates repeatability counts.
    frame = frame.sort_values([*JOIN_COLUMNS, "iteration_id"])
    return frame.drop_duplicates(JOIN_COLUMNS, keep="first").reset_index(drop=True)


def _gap_pct(peak: object, sustained: object) -> float | None:
    try:
        peak_f = float(peak)  # type: ignore[arg-type]
        sustained_f = float(sustained)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(peak_f) or not math.isfinite(sustained_f) or sustained_f <= 0:
        return None
    return 100.0 * (peak_f - sustained_f) / sustained_f


def _add_run_metrics(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Add explicit max/peak, sustained, and peak-vs-sustained columns."""
    out = frame.copy()
    peak = f"{prefix}_explore_peak_goodput_rps"
    sustained = f"{prefix}_sustained_goodput_rps"
    out[f"{prefix}_max_goodput_rps"] = out[peak]
    out[f"{prefix}_peak_sustained_gap_rps"] = out[peak] - out[sustained]
    out[f"{prefix}_peak_sustained_gap_pct"] = [
        _gap_pct(p, s) for p, s in zip(out[peak], out[sustained])
    ]
    out[f"{prefix}_sustained_positive"] = out[sustained].gt(0)
    return out


def pair_runs(original: pd.DataFrame, reverified: pd.DataFrame) -> pd.DataFrame:
    """Outer-join two run tables and derive paired repeatability metrics."""
    original = original.rename(
        columns={c: f"original_{c}" for c in original.columns if c not in JOIN_COLUMNS}
    )
    reverified = reverified.rename(
        columns={c: f"reverified_{c}" for c in reverified.columns if c not in JOIN_COLUMNS}
    )
    merged = original.merge(
        reverified,
        on=JOIN_COLUMNS,
        how="outer",
        indicator="pair_status",
        sort=True,
    )
    merged = _add_run_metrics(merged, "original")
    merged = _add_run_metrics(merged, "reverified")

    orig_s = merged["original_sustained_goodput_rps"]
    re_s = merged["reverified_sustained_goodput_rps"]
    orig_p = merged["original_explore_peak_goodput_rps"]
    re_p = merged["reverified_explore_peak_goodput_rps"]
    both_s = orig_s.notna() & re_s.notna()
    both_positive = both_s & orig_s.gt(0) & re_s.gt(0)

    merged["sustained_delta_rps"] = re_s - orig_s
    merged["sustained_abs_delta_rps"] = (re_s - orig_s).abs()
    merged["sustained_relative_delta_pct"] = (re_s - orig_s).div(orig_s).mul(100).where(orig_s.gt(0))
    merged["sustained_abs_relative_delta_pct"] = merged["sustained_relative_delta_pct"].abs()
    merged["sustained_symmetric_pct_difference"] = (
        (re_s - orig_s).abs().div((re_s + orig_s) / 2).mul(100).where(both_positive)
    )
    merged["max_goodput_delta_rps"] = re_p - orig_p
    merged["max_goodput_abs_delta_rps"] = (re_p - orig_p).abs()
    merged["max_goodput_relative_delta_pct"] = (re_p - orig_p).div(orig_p).mul(100).where(orig_p.gt(0))
    merged["peak_sustained_gap_delta_pct_points"] = (
        merged["reverified_peak_sustained_gap_pct"]
        - merged["original_peak_sustained_gap_pct"]
    )
    merged["paired_sustained"] = both_s
    merged["paired_positive_sustained"] = both_positive
    both_artifacts = merged["original_bench_log_sha256"].notna() & merged[
        "reverified_bench_log_sha256"
    ].notna()
    merged["artifact_comparison"] = "unmatched"
    merged.loc[merged["pair_status"].eq("both") & ~both_artifacts, "artifact_comparison"] = (
        "unknown"
    )
    # A byte-identical bench log is evidence that this archive contains the
    # same result rather than a second measurement.  The explicit assignment
    # avoids treating incomplete/copy-only rows as repeatability evidence.
    equal_logs = both_artifacts & merged["original_bench_log_sha256"].eq(
        merged["reverified_bench_log_sha256"]
    )
    merged.loc[equal_logs, "artifact_comparison"] = "unchanged"
    merged.loc[both_artifacts & ~equal_logs, "artifact_comparison"] = "changed"
    return merged


def _finite_values(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return values[values.map(math.isfinite)]


def _metric_stats(series: pd.Series, prefix: str) -> dict[str, float | int | None]:
    values = _finite_values(series)
    if values.empty:
        return {
            f"{prefix}_n": 0,
            f"{prefix}_mean": None,
            f"{prefix}_median": None,
            f"{prefix}_std": None,
            f"{prefix}_variance": None,
            f"{prefix}_min": None,
            f"{prefix}_max": None,
            f"{prefix}_p25": None,
            f"{prefix}_p75": None,
        }
    variance = float(values.var(ddof=1)) if len(values) > 1 else None
    std = math.sqrt(variance) if variance is not None else None
    return {
        f"{prefix}_n": int(len(values)),
        f"{prefix}_mean": float(values.mean()),
        f"{prefix}_median": float(values.median()),
        f"{prefix}_std": std,
        f"{prefix}_variance": variance,
        f"{prefix}_min": float(values.min()),
        f"{prefix}_max": float(values.max()),
        f"{prefix}_p25": float(values.quantile(0.25)),
        f"{prefix}_p75": float(values.quantile(0.75)),
    }


def _summary_row(group: pd.DataFrame, *, grouping: str, labels: dict[str, object]) -> dict[str, object]:
    paired = group[group["paired_sustained"]]
    positive = group[group["paired_positive_sustained"]]
    candidate_pairs = group[group["pair_status"] == "both"]
    row: dict[str, object] = {
        "grouping": grouping,
        **labels,
        "n_original_runs": int(group["original_iteration_id"].notna().sum()),
        "n_reverified_runs": int(group["reverified_iteration_id"].notna().sum()),
        "n_candidate_pairs": int(len(candidate_pairs)),
        "n_paired_runs": int(len(paired)),
        "n_paired_positive_sustained": int(len(positive)),
        "n_original_positive_sustained": int(group["original_sustained_goodput_rps"].gt(0).sum()),
        "n_reverified_positive_sustained": int(group["reverified_sustained_goodput_rps"].gt(0).sum()),
    }
    for column, prefix in (
        ("original_sustained_goodput_rps", "original_sustained"),
        ("reverified_sustained_goodput_rps", "reverified_sustained"),
        ("original_max_goodput_rps", "original_max_goodput"),
        ("reverified_max_goodput_rps", "reverified_max_goodput"),
        ("sustained_delta_rps", "sustained_delta_rps"),
        ("sustained_abs_delta_rps", "sustained_abs_delta_rps"),
        ("sustained_relative_delta_pct", "sustained_relative_delta_pct"),
        ("sustained_abs_relative_delta_pct", "sustained_abs_relative_delta_pct"),
        ("sustained_symmetric_pct_difference", "sustained_symmetric_pct_difference"),
        ("max_goodput_delta_rps", "max_goodput_delta_rps"),
        ("max_goodput_relative_delta_pct", "max_goodput_relative_delta_pct"),
        ("original_peak_sustained_gap_pct", "original_peak_sustained_gap_pct"),
        ("reverified_peak_sustained_gap_pct", "reverified_peak_sustained_gap_pct"),
        ("peak_sustained_gap_delta_pct_points", "peak_sustained_gap_delta_pct_points"),
    ):
        # Repeatability statistics must describe the fixed candidates that
        # were measured in both trees; unmatched rows remain visible through
        # the counts above but cannot contribute to a paired variance.
        row.update(_metric_stats(paired[column], prefix))
    return row


def summarize(pairs: pd.DataFrame) -> pd.DataFrame:
    """Return overall plus model, network-evidence, and artifact summaries."""
    rows: list[dict[str, object]] = []
    rows.append(_summary_row(pairs, grouping="overall", labels={}))
    for grouping, columns in (
        ("model", ["model"]),
        ("model_env", ["model", "env"]),
        ("network", ["original_locust_master_network"]),
        ("historical_phase", ["original_historical_network_phase"]),
        ("artifact", ["artifact_comparison"]),
        ("network_artifact", ["original_locust_master_network", "artifact_comparison"]),
        (
            "historical_phase_artifact",
            ["original_historical_network_phase", "artifact_comparison"],
        ),
        (
            "model_historical_phase_artifact",
            ["model", "original_historical_network_phase", "artifact_comparison"],
        ),
    ):
        for values, group in pairs.groupby(columns, dropna=False, sort=True):
            if not isinstance(values, tuple):
                values = (values,)
            labels = dict(zip(columns, values))
            rows.append(_summary_row(group, grouping=grouping, labels=labels))
    return pd.DataFrame(rows)


def _print_summary(summary: pd.DataFrame) -> None:
    columns = [
        "grouping",
        "model",
        "env",
        "original_locust_master_network",
        "original_historical_network_phase",
        "artifact_comparison",
        "n_candidate_pairs",
        "n_paired_runs",
        "n_paired_positive_sustained",
        "sustained_abs_relative_delta_pct_median",
        "sustained_abs_relative_delta_pct_mean",
        "sustained_delta_rps_mean",
        "sustained_delta_rps_std",
        "max_goodput_relative_delta_pct_median",
        "original_sustained_variance",
        "reverified_sustained_variance",
        "original_peak_sustained_gap_pct_median",
        "reverified_peak_sustained_gap_pct_median",
    ]
    available = [c for c in columns if c in summary.columns]
    print(summary[available].round(2).to_string(index=False))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--original-root", type=Path, default=_REPO_ROOT / "results")
    ap.add_argument("--reverified-root", type=Path, default=_REPO_ROOT / "results_reverified")
    ap.add_argument("--experiment-slug", default="results")
    ap.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "results_aggregate" / "repeatability")
    ap.add_argument("--include-models", nargs="*", default=None)
    ap.add_argument("--exclude-models", nargs="*", default=None)
    args = ap.parse_args(argv)

    for label, root in (("original", args.original_root), ("reverified", args.reverified_root)):
        if not root.is_dir():
            print(f"{label} results root not found: {root}", file=sys.stderr)
            return 2

    include = set(args.include_models) if args.include_models else None
    exclude = set(args.exclude_models) if args.exclude_models else None
    original = collect_runs(
        args.original_root,
        experiment_slug=args.experiment_slug,
        include_models=include,
        exclude_models=exclude,
    )
    reverified = collect_runs(
        args.reverified_root,
        experiment_slug=args.experiment_slug,
        include_models=include,
        exclude_models=exclude,
    )
    if original.empty and reverified.empty:
        print("No parseable bench.log files found in either results tree.", file=sys.stderr)
        return 1

    pairs = pair_runs(original, reverified)
    summary = summarize(pairs)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    pairs_path = args.out_dir / "pairs.csv"
    summary_path = args.out_dir / "summary.csv"
    pairs.to_csv(pairs_path, index=False)
    summary.to_csv(summary_path, index=False)
    print(f"Wrote {pairs_path} ({len(pairs)} rows)")
    print(f"Wrote {summary_path} ({len(summary)} rows)")
    print(
        "Pair status: "
        + pairs["pair_status"].value_counts(dropna=False).to_dict().__repr__()
    )
    print("\n== Repeatability summary ==")
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

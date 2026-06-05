import argparse
import pathlib
from pathlib import Path

import pandas as pd

from distributed_bench.load_profiles.registry import LOAD_PROFILE_REGISTRY
from distributed_bench.system_configs.registry import SYSTEM_TOPOLOGY_REGISTRY


def process_benchmark(stats_csv_path, sla_ms=300.0, transition_trim_s=15):
    """
    Processes a single locust stats_history.csv file.
    Returns peak RPS and a DataFrame with the step-by-step breakdown.
    """
    try:
        df = pd.read_csv(stats_csv_path)
    except Exception as e:
        print(f"Error reading {stats_csv_path}: {e}")
        return None

    # We only care about the overall totals per second, which are labelled 'Aggregated'
    if "Name" not in df.columns:
        print(f"Missing 'Name' column in {stats_csv_path}")
        return None

    df_agg = df[df["Name"] == "Aggregated"].copy()

    if df_agg.empty:
        return None

    # Sort by timestamp
    df_agg = df_agg.sort_values("Timestamp")

    results = []

    # Group by the User Count to evaluate each step
    for user_count, group in df_agg.groupby("User Count"):
        if user_count == 0:
            continue

        # Discard the first N seconds of the step to ignore ramp-up/TCP connection transition
        if len(group) <= transition_trim_s:
            # If the step is too short to trim, we'll skip evaluating it or it wasn't a real step
            continue

        plateau = group.iloc[transition_trim_s:]

        # Parse the 99% latency column (coerce errors because empty/initial rows might have 'N/A')
        p99_col = pd.to_numeric(plateau["99%"], errors="coerce")
        if p99_col.isna().all():
            continue

        # 90th percentile of the rolling 99th percentiles
        step_p99_p90 = p99_col.quantile(0.90)

        # Max failure rate during the plateau
        max_failures = pd.to_numeric(plateau["Failures/s"], errors="coerce").max()

        # Average actual Requests/s achieved during the plateau
        avg_rps = pd.to_numeric(plateau["Requests/s"], errors="coerce").mean()

        results.append(
            {
                "User Count": user_count,
                "Step p99 (p90)": step_p99_p90,
                "Max Failures/s": max_failures,
                "Avg RPS": avg_rps,
            }
        )

    if not results:
        return None

    results_df = pd.DataFrame(results)

    # Find steps that meet our criteria:
    # 1. P99 latency <= SLA threshold
    # 2. Zero failures
    valid_steps = results_df[
        (results_df["Step p99 (p90)"] <= sla_ms) & (results_df["Max Failures/s"] == 0)
    ]

    if valid_steps.empty:
        return 0.0, results_df

    # The peak sustained throughput is the RPS from the valid step with the highest User Count
    best_step = valid_steps.loc[valid_steps["User Count"].idxmax()]

    return best_step["Avg RPS"], results_df


def _infer_from_perf_dir(perf_dir_name: str) -> tuple[str | None, str | None]:
    """
    Best-effort extraction of (system_topology, load_profile) from a perf directory name.
    Expected format: perf-<topology>-<load_profile>-<YYYYMMDD>-<HHMMSS>

    We don't try to parse by splitting because both topology and profile contain '-'.
    Instead, match known registry keys inside the name.
    """
    sys_name = None
    for key in sorted(SYSTEM_TOPOLOGY_REGISTRY.keys(), key=len, reverse=True):
        if f"-{key}-" in perf_dir_name:
            sys_name = key
            break

    prof_name = None
    for key in sorted(LOAD_PROFILE_REGISTRY.keys(), key=len, reverse=True):
        if f"-{key}-" in perf_dir_name:
            prof_name = key
            break

    return sys_name, prof_name


def _find_results_root(p: Path) -> Path | None:
    """
    Walk upwards and return the nearest ancestor directory named 'results'.
    Returns None if not found.
    """
    cur = p.resolve()
    for parent in [cur, *cur.parents]:
        if parent.name == "results":
            return parent
    return None


def _safe_slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in ("-", "_", ".", "=")) else "_" for c in s).strip("_") or "default"


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_text(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", errors="ignore")


def _bench_analysis_root(base_path: Path, all_stats_files: list[Path] | None = None) -> Path:
    """
    Root directory for throughput analysis outputs.

    We intentionally keep this under results/ and prefix with 0_ so it sorts first.
    """
    results_root = _find_results_root(base_path)
    if results_root is None and all_stats_files:
        results_root = _find_results_root(all_stats_files[0])
    if results_root is None:
        return base_path.resolve() / "0_bench-analysis"
    return results_root / "0_bench-analysis"


def _analysis_id_dirname(analysis_id: str) -> str | None:
    """
    Avoid cluttering paths with a trailing 'default' directory.
    """
    aid = (analysis_id or "").strip()
    if not aid or aid == "default":
        return None
    return _safe_slug(aid)


def _render_metric_table(pivot_df: pd.DataFrame, *, title: str) -> str:
    """
    Render a console-like table (no ANSI colors) into plain text.
    """
    if pivot_df.empty:
        return f"--- {title} ---\n(no data)\n"

    models = list(pivot_df.columns)
    index_name = pivot_df.index.name or "Scenario-Environment"

    idx_width = max(len(index_name), int(pivot_df.index.astype(str).map(len).max()))
    col_widths = [idx_width] + [max(len(m), 8) for m in models]

    header = f"{index_name:<{col_widths[0]}} | " + " | ".join(
        f"{m:>{w}}" for m, w in zip(models, col_widths[1:])
    )
    lines = [f"--- {title} ---", header, "-" * len(header)]

    for idx, row in pivot_df.iterrows():
        row_str = f"{str(idx):<{col_widths[0]}} | "
        vals_str: list[str] = []
        for m, w in zip(models, col_widths[1:]):
            val = row[m]
            if pd.isna(val):
                vals_str.append(f"{'N/A':>{w}}")
            else:
                vals_str.append(f"{float(val):>{w}.2f}")
        lines.append(row_str + " | ".join(vals_str))
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _render_system_compare_table(pivot_df: pd.DataFrame, *, title: str) -> str:
    """
    Same renderer as _render_metric_table, but with a slightly clearer default index name
    for system-level comparisons.
    """
    if pivot_df.empty:
        return f"--- {title} ---\n(no data)\n"

    cols = list(pivot_df.columns)
    index_name = pivot_df.index.name or "Scenario-Environment"

    idx_width = max(len(index_name), int(pivot_df.index.astype(str).map(len).max()))
    col_widths = [idx_width] + [max(len(str(c)), 8) for c in cols]

    header = f"{index_name:<{col_widths[0]}} | " + " | ".join(
        f"{str(c):>{w}}" for c, w in zip(cols, col_widths[1:])
    )
    lines = [f"--- {title} ---", header, "-" * len(header)]

    for idx, row in pivot_df.iterrows():
        row_str = f"{str(idx):<{col_widths[0]}} | "
        vals_str: list[str] = []
        for c, w in zip(cols, col_widths[1:]):
            val = row[c]
            if pd.isna(val):
                vals_str.append(f"{'N/A':>{w}}")
            else:
                vals_str.append(f"{float(val):>{w}.2f}")
        lines.append(row_str + " | ".join(vals_str))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Analyze peak sustained throughput from Locust logs."
    )
    parser.add_argument(
        "path",
        type=str,
        help="Path to a single results directory, or a root directory containing multiple results.",
    )
    parser.add_argument(
        "--sla",
        type=float,
        default=300.0,
        help="SLA threshold for p99 latency in ms (default: 300).",
    )
    parser.add_argument(
        "--trim",
        type=int,
        default=15,
        help="Number of seconds to trim from the start of each step (default: 15).",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Filter by load profile name (e.g., 'stairs-fine-stress').",
    )
    parser.add_argument(
        "--system-config",
        type=str,
        default=None,
        help="System configuration / topology name (e.g., '3C-2B-1DB'). If omitted, inferred from perf dir name.",
    )
    parser.add_argument(
        "--load-profile",
        type=str,
        default=None,
        help="Load profile name (e.g., 'stairs-1500-100-30-15'). If omitted, inferred from perf dir name.",
    )
    parser.add_argument(
        "--analysis-id",
        type=str,
        default="default",
        help="Analysis identifier to separate outputs (default: 'default').",
    )
    args = parser.parse_args(argv)

    base_path = Path(args.path)

    if not base_path.exists():
        print(f"Error: Path does not exist: {base_path}")
        return

    # Check if the provided path is a single benchmark directory containing the stats_history file
    stats_files = list((base_path / "locust" / "results").glob("*_stats_history.csv"))

    if stats_files:
        # SINGLE DIRECTORY MODE (Sanity Check)
        stats_file = stats_files[0]
        print(f"Analyzing single benchmark: {base_path}\n")
        res = process_benchmark(stats_file, args.sla, args.trim)

        if res is not None:
            peak_rps, df_steps = res

            # Format DataFrame for nice printing
            df_steps["Step p99 (p90)"] = df_steps["Step p99 (p90)"].round(2)
            df_steps["Max Failures/s"] = df_steps["Max Failures/s"].round(2)
            df_steps["Avg RPS"] = df_steps["Avg RPS"].round(2)

            print("--- Step-by-Step Breakdown ---")
            print(df_steps.to_string(index=False))
            print("-" * 30)
            print(f"==> Peak Sustained Throughput: {peak_rps:.2f} RPS (SLA: {args.sla}ms)")
        else:
            print("No valid stats found in this directory.")

    else:
        # RECURSIVE CONSOLIDATED MODE
        print(f"Scanning for benchmarks in {base_path}...\n")
        all_stats_files = list(base_path.rglob("locust/results/*_stats_history.csv"))

        if args.profile:
            all_stats_files = [f for f in all_stats_files if args.profile in str(f)]

        if not all_stats_files:
            print("No benchmark stats history files found matching the criteria.")
            return

        summary = []
        for f in all_stats_files:
            res = process_benchmark(f, args.sla, args.trim)
            if res is not None:
                peak_rps, _ = res

                # Current structure is typically:
                # results/<MODEL>/<SCENARIO>/<ENV>/<PROMPT>/<SAMPLE>/<PERF_DIR>/locust/results/<test>_stats_history.csv
                # Keep fallbacks for other layouts.
                parts = f.parts
                model = "unknown"
                scenario = "unknown"
                env = "unknown"
                prompt = "unknown"
                sample_str = "unknown"
                # f = .../<PERF_DIR>/locust/results/<test>_stats_history.csv
                perf_dir = f.parent.parent.parent

                results_root = _find_results_root(f)
                if results_root is not None:
                    rel = f.resolve().relative_to(results_root.resolve()).parts
                    if len(rel) >= 6:
                        model, scenario, env, prompt, sample_str = rel[0], rel[1], rel[2], rel[3], rel[4]
                else:
                    # Fallback heuristics — locust/results adds two trailing segments
                    model = parts[-9] if len(parts) >= 9 else model
                    scenario = parts[-8] if len(parts) >= 8 else scenario
                    env = parts[-7] if len(parts) >= 7 else env
                    prompt = parts[-6] if len(parts) >= 6 else prompt
                    for p in parts:
                        if p.startswith("sample"):
                            sample_str = p
                            break

                inferred_sys, inferred_prof = _infer_from_perf_dir(perf_dir.name)
                sys_name = args.system_config or inferred_sys or "unknown-system"
                prof_name = args.load_profile or args.profile or inferred_prof or "unknown-profile"

                summary.append(
                    {
                        "Model": model,
                        "Scenario": scenario,
                        "Environment": env,
                        "Prompt": prompt,
                        "Sample": sample_str,
                        "System Config": sys_name,
                        "Load Profile": prof_name,
                        "Peak Sustained RPS": round(peak_rps, 2),
                        "Path": str(f.parent),
                    }
                )

        if summary:
            summary_df = pd.DataFrame(summary)
            # Sort for readability
            summary_df = summary_df.sort_values(
                by=[
                    "System Config",
                    "Load Profile",
                    "Scenario",
                    "Environment",
                    "Prompt",
                    "Model",
                    "Sample",
                ]
            )

            print("--- Consolidated Summary (split by load profile) ---")
            print(
                summary_df[
                    ["System Config", "Load Profile", "Scenario", "Environment", "Model", "Peak Sustained RPS"]
                ].to_string(index=False)
            )

            # Write separate overview outputs per (system config, load profile).
            overview_root = _bench_analysis_root(base_path, all_stats_files)

            # Collect best@3 for a top-level comparison report.
            # (Keyed by load profile, model, scenario-environment, system config)
            system_compare_rows: list[dict] = []

            for (sys_name, prof_name), group_df in summary_df.groupby(["System Config", "Load Profile"]):
                out_dir = overview_root / "overviews" / _safe_slug(sys_name) / _safe_slug(prof_name)
                aid_dir = _analysis_id_dirname(args.analysis_id)
                if aid_dir:
                    out_dir = out_dir / aid_dir
                _ensure_dir(out_dir)

                # Keep CSVs if useful, but put them under a dedicated folder.
                csv_dir = out_dir / "csv"
                _ensure_dir(csv_dir)
                out_file = csv_dir / "throughput_summary.csv"
                group_df.to_csv(out_file, index=False)
                print(f"\n==> Saved summary CSV to {out_file}")

                # Compute best@1, best@3, worst@3 within this profile/system group
                metrics_data = []
                grouped = group_df.groupby(["Scenario", "Environment", "Model"])
                for name, sub in grouped:
                    scen, env, mod = name

                    # Determine how many samples were actually attempted by looking at the filesystem
                    attempted_samples = set()
                    if not sub.empty:
                        first_path = pathlib.Path(sub["Path"].iloc[0])
                        base_dir = first_path
                        while base_dir.name and not base_dir.name.startswith("sample"):
                            base_dir = base_dir.parent
                        if base_dir.name.startswith("sample"):
                            base_dir = base_dir.parent
                            if base_dir.exists():
                                for p in base_dir.iterdir():
                                    if p.is_dir() and p.name.startswith("sample"):
                                        attempted_samples.add(p.name)

                    # Map them to their RPS, default to 0.0 if not in group (meaning it failed earlier)
                    rps_by_sample = {}
                    for s in attempted_samples:
                        s_data = sub[sub["Sample"] == s]["Peak Sustained RPS"]
                        if not s_data.empty:
                            rps_by_sample[s] = s_data.max()
                        else:
                            rps_by_sample[s] = 0.0

                    best_1 = rps_by_sample.get("sample0", float("nan"))
                    s_012_vals = [rps_by_sample[s] for s in ["sample0", "sample1", "sample2"] if s in rps_by_sample]
                    best_3 = max(s_012_vals) if s_012_vals else float("nan")
                    worst_3 = min(s_012_vals) if s_012_vals else float("nan")

                    metrics_data.append(
                        {
                            "Scenario": scen,
                            "Environment": env,
                            "Model": mod,
                            "best@1": best_1,
                            "best@3": best_3,
                            "worst@3": worst_3,
                        }
                    )

                metrics_df = pd.DataFrame(metrics_data)
                metrics_out_file = csv_dir / "throughput_summary_metrics.csv"
                metrics_df.to_csv(metrics_out_file, index=False)
                print(f"==> Saved metrics summary CSV to {metrics_out_file}")

                # Also write a human-readable text report (similar to console output).
                metrics_df["Scenario-Environment"] = metrics_df["Scenario"] + "-" + metrics_df["Environment"]

                report_parts: list[str] = []
                report_parts.append(f"system_config={sys_name}")
                report_parts.append(f"load_profile={prof_name}")
                if _analysis_id_dirname(args.analysis_id):
                    report_parts.append(f"analysis_id={args.analysis_id}")
                report_parts.append(f"sla_ms={args.sla}")
                report_parts.append(f"trim_s={args.trim}")
                report_parts.append("")

                for metric in ["best@1", "best@3", "worst@3"]:
                    pivot_df = metrics_df.pivot(index="Scenario-Environment", columns="Model", values=metric)
                    report_parts.append(_render_metric_table(pivot_df, title=f"{metric} Peak Sustained RPS"))

                # Visually separate rollups from the tables above.
                report_parts.extend(["", "", "", ""])  # total four blank lines

                # best@3 summary across environments (frameworks) for each scenario
                if not metrics_df.empty:
                    scen_summary = (
                        metrics_df.groupby(["Scenario", "Model"], dropna=False)["best@3"]
                        .mean()
                        .reset_index()
                    )
                    scen_pivot = scen_summary.pivot(index="Scenario", columns="Model", values="best@3")
                    report_parts.append(
                        _render_metric_table(
                            scen_pivot,
                            title="best@3 (avg over frameworks) by Scenario",
                        )
                    )

                    # best@3 summary across scenarios for each environment (framework)
                    fw_summary = (
                        metrics_df.groupby(["Environment", "Model"], dropna=False)["best@3"]
                        .mean()
                        .reset_index()
                    )
                    fw_pivot = fw_summary.pivot(index="Environment", columns="Model", values="best@3")
                    report_parts.append(
                        _render_metric_table(
                            fw_pivot,
                            title="best@3 (avg over scenarios) by Framework",
                        )
                    )

                    # Collect for global system comparison.
                    for _, r in metrics_df[["Scenario-Environment", "Model", "best@3"]].iterrows():
                        system_compare_rows.append(
                            {
                                "Load Profile": prof_name,
                                "System Config": sys_name,
                                "Scenario-Environment": r["Scenario-Environment"],
                                "Model": r["Model"],
                                "best@3": r["best@3"],
                            }
                        )

                report_text = "\n".join(report_parts).rstrip() + "\n"
                report_path = out_dir / "throughput_summary.txt"
                _write_text(report_path, report_text)
                print(f"==> Saved human-readable summary to {report_path}")

            # Write a top-level system comparison report for best@3.
            if system_compare_rows:
                sys_df = pd.DataFrame(system_compare_rows)

                out_dir = overview_root / "overviews"
                aid_dir = _analysis_id_dirname(args.analysis_id)
                if aid_dir:
                    out_dir = out_dir / aid_dir
                _ensure_dir(out_dir)

                parts: list[str] = []
                parts.append("system_throughput_summary")
                if _analysis_id_dirname(args.analysis_id):
                    parts.append(f"analysis_id={args.analysis_id}")
                parts.append(f"sla_ms={args.sla}")
                parts.append(f"trim_s={args.trim}")
                parts.append("")

                # Per load profile: compare system configs side-by-side, per model.
                for prof_name, prof_df in sys_df.groupby("Load Profile"):
                    parts.append(f"load_profile={prof_name}")
                    parts.append("")

                    for model, m_df in prof_df.groupby("Model"):
                        pivot = m_df.pivot(
                            index="Scenario-Environment",
                            columns="System Config",
                            values="best@3",
                        )
                        parts.append(
                            _render_system_compare_table(
                                pivot,
                                title=f"best@3 Peak Sustained RPS (Model={model})",
                            )
                        )

                        # Scenario-level comparison (avg across environments)
                        scen_df = (
                            m_df.assign(Scenario=m_df["Scenario-Environment"].astype(str).str.split("-", n=1).str[0])
                            .groupby(["Scenario", "System Config"], dropna=False)["best@3"]
                            .mean()
                            .reset_index()
                        )
                        scen_pivot = scen_df.pivot(index="Scenario", columns="System Config", values="best@3")
                        parts.append(
                            _render_system_compare_table(
                                scen_pivot,
                                title=f"best@3 by Scenario (avg over frameworks, Model={model})",
                            )
                        )

                        # Framework-level comparison (avg across scenarios)
                        fw_df = (
                            m_df.assign(Framework=m_df["Scenario-Environment"].astype(str).str.split("-", n=1).str[1])
                            .groupby(["Framework", "System Config"], dropna=False)["best@3"]
                            .mean()
                            .reset_index()
                        )
                        fw_pivot = fw_df.pivot(index="Framework", columns="System Config", values="best@3")
                        parts.append(
                            _render_system_compare_table(
                                fw_pivot,
                                title=f"best@3 by Framework (avg over scenarios, Model={model})",
                            )
                        )

                    parts.append("")  # spacer between profiles

                sys_report = "\n".join(parts).rstrip() + "\n"
                sys_report_path = out_dir / "system_throughput_summary.txt"
                _write_text(sys_report_path, sys_report)
                print(f"==> Saved system comparison summary to {sys_report_path}")


if __name__ == "__main__":
    main()


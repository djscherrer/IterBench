import argparse
import pathlib
import pandas as pd
from pathlib import Path

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
    if 'Name' not in df.columns:
        print(f"Missing 'Name' column in {stats_csv_path}")
        return None
        
    df_agg = df[df['Name'] == 'Aggregated'].copy()
    
    if df_agg.empty:
        return None
    
    # Sort by timestamp
    df_agg = df_agg.sort_values('Timestamp')
    
    results = []
    
    # Group by the User Count to evaluate each step
    for user_count, group in df_agg.groupby('User Count'):
        if user_count == 0:
            continue
            
        # Discard the first N seconds of the step to ignore ramp-up/TCP connection transition
        if len(group) <= transition_trim_s:
            # If the step is too short to trim, we'll skip evaluating it or it wasn't a real step
            continue
            
        plateau = group.iloc[transition_trim_s:]
        
        # Parse the 99% latency column (coerce errors because empty/initial rows might have 'N/A')
        p99_col = pd.to_numeric(plateau['99%'], errors='coerce')
        if p99_col.isna().all():
            continue
            
        # 90th percentile of the rolling 99th percentiles
        step_p99_p90 = p99_col.quantile(0.90)
        
        # Max failure rate during the plateau
        max_failures = pd.to_numeric(plateau['Failures/s'], errors='coerce').max()
        
        # Average actual Requests/s achieved during the plateau
        avg_rps = pd.to_numeric(plateau['Requests/s'], errors='coerce').mean()
        
        results.append({
            'User Count': user_count,
            'Step p99 (p90)': step_p99_p90,
            'Max Failures/s': max_failures,
            'Avg RPS': avg_rps
        })
        
    if not results:
        return None
        
    results_df = pd.DataFrame(results)
    
    # Find steps that meet our criteria:
    # 1. P99 latency <= SLA threshold
    # 2. Zero failures
    valid_steps = results_df[(results_df['Step p99 (p90)'] <= sla_ms) & (results_df['Max Failures/s'] == 0)]
    
    if valid_steps.empty:
        return 0.0, results_df
        
    # The peak sustained throughput is the maximum RPS achieved across all valid steps
    best_step = valid_steps.loc[valid_steps['Avg RPS'].idxmax()]
    
    return best_step['Avg RPS'], results_df

def main():
    parser = argparse.ArgumentParser(description="Analyze peak sustained throughput from Locust logs.")
    parser.add_argument('path', type=str, help="Path to a single results directory, or a root directory containing multiple results.")
    parser.add_argument('--sla', type=float, default=300.0, help="SLA threshold for p99 latency in ms (default: 300).")
    parser.add_argument('--trim', type=int, default=15, help="Number of seconds to trim from the start of each step (default: 15).")
    parser.add_argument('--profile', type=str, default=None, help="Filter by load profile name (e.g., 'stairs-fine-stress').")
    args = parser.parse_args()
    
    base_path = Path(args.path)
    
    if not base_path.exists():
        print(f"Error: Path does not exist: {base_path}")
        return
        
    # Check if the provided path is a single benchmark directory containing the stats_history file
    stats_files = list(base_path.glob("bench_results_*_stats_history.csv"))
    
    if stats_files:
        # SINGLE DIRECTORY MODE (Sanity Check)
        stats_file = stats_files[0]
        print(f"Analyzing single benchmark: {base_path}\n")
        res = process_benchmark(stats_file, args.sla, args.trim)
        
        if res is not None:
            peak_rps, df_steps = res
            
            # Format DataFrame for nice printing
            df_steps['Step p99 (p90)'] = df_steps['Step p99 (p90)'].round(2)
            df_steps['Max Failures/s'] = df_steps['Max Failures/s'].round(2)
            df_steps['Avg RPS'] = df_steps['Avg RPS'].round(2)
            
            print("--- Step-by-Step Breakdown ---")
            print(df_steps.to_string(index=False))
            print("-" * 30)
            print(f"==> Peak Sustained Throughput: {peak_rps:.2f} RPS (SLA: {args.sla}ms)")
        else:
            print("No valid stats found in this directory.")
            
    else:
        # RECURSIVE CONSOLIDATED MODE
        print(f"Scanning for benchmarks in {base_path}...\n")
        all_stats_files = list(base_path.rglob("bench_results_*_stats_history.csv"))
        
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
                
                # Attempt to extract context from path. Structure is typically:
                # results_snapshots/DATE/MODEL/SCENARIO/ENV/PROMPT/SAMPLE/BENCH_DIR
                parts = f.parts
                sample_str = "unknown"
                try:
                    idx = parts.index("results_snapshots")
                    model = parts[idx+2]
                    scenario = parts[idx+3]
                    env = parts[idx+4]
                    for p in parts[idx+5:]:
                        if p.startswith("sample"):
                            sample_str = p
                            break
                except (ValueError, IndexError):
                    # Fallback
                    model = parts[-7] if len(parts) >= 7 else "unknown"
                    scenario = parts[-6] if len(parts) >= 6 else "unknown"
                    env = parts[-5] if len(parts) >= 5 else "unknown"
                    for p in parts:
                        if p.startswith("sample"):
                            sample_str = p
                            break

                summary.append({
                    'Model': model,
                    'Scenario': scenario,
                    'Environment': env,
                    'Sample': sample_str,
                    'Peak Sustained RPS': round(peak_rps, 2),
                    'Path': str(f.parent)
                })
                
        if summary:
            summary_df = pd.DataFrame(summary)
            # Sort for readability
            summary_df = summary_df.sort_values(by=['Scenario', 'Environment', 'Model', 'Sample'])
            
            summary_df['Scenario-Environment'] = summary_df['Scenario'] + '-' + summary_df['Environment']
            print("--- Consolidated Summary ---")
            print(summary_df[['Scenario-Environment', 'Sample', 'Model', 'Peak Sustained RPS']].to_string(index=False))
            
            out_file = "throughput_summary.csv"
            summary_df.to_csv(out_file, index=False)
            print(f"\n==> Saved full consolidated summary to {out_file}")
            
            # Compute best@1, best@3, worst@3
            metrics_data = []
            grouped = summary_df.groupby(['Scenario', 'Environment', 'Model'])
            for name, group in grouped:
                scen, env, mod = name
                
                # Determine how many samples were actually attempted by looking at the filesystem
                attempted_samples = set()
                if not group.empty:
                    first_path = pathlib.Path(group['Path'].iloc[0])
                    base_dir = first_path
                    while base_dir.name and not base_dir.name.startswith('sample'):
                        base_dir = base_dir.parent
                    if base_dir.name.startswith('sample'):
                        base_dir = base_dir.parent
                        if base_dir.exists():
                            for p in base_dir.iterdir():
                                if p.is_dir() and p.name.startswith('sample'):
                                    attempted_samples.add(p.name)
                
                # Map them to their RPS, default to 0.0 if not in group (meaning it failed earlier)
                rps_by_sample = {}
                for s in attempted_samples:
                    s_data = group[group['Sample'] == s]['Peak Sustained RPS']
                    if not s_data.empty:
                        rps_by_sample[s] = s_data.max()
                    else:
                        rps_by_sample[s] = 0.0
                
                if 'sample0' in rps_by_sample:
                    best_1 = rps_by_sample['sample0']
                else:
                    best_1 = float('nan')
                
                s_012_vals = [rps_by_sample[s] for s in ['sample0', 'sample1', 'sample2'] if s in rps_by_sample]
                
                best_3 = max(s_012_vals) if s_012_vals else float('nan')
                worst_3 = min(s_012_vals) if s_012_vals else float('nan')
                
                metrics_data.append({
                    'Scenario': scen,
                    'Environment': env,
                    'Model': mod,
                    'best@1': best_1,
                    'best@3': best_3,
                    'worst@3': worst_3
                })
            
            metrics_df = pd.DataFrame(metrics_data)
            metrics_out_file = "throughput_summary_metrics.csv"
            metrics_df.to_csv(metrics_out_file, index=False)
            print(f"\n==> Saved metrics summary to {metrics_out_file}")

            metrics_df['Scenario-Environment'] = metrics_df['Scenario'] + '-' + metrics_df['Environment']
            GREEN = '\033[92m'
            RESET = '\033[0m'
            
            for metric in ['best@1', 'best@3', 'worst@3']:
                print(f"\n--- {metric} Peak Sustained RPS ---")
                pivot_df = metrics_df.pivot(index='Scenario-Environment', columns='Model', values=metric)
                
                models = list(pivot_df.columns)
                col_widths = [max(len("Scenario-Environment"), pivot_df.index.astype(str).map(len).max())]
                for model in models:
                    col_widths.append(max(len(model), 8))
                    
                header = f"{'Scenario-Environment':<{col_widths[0]}} | " + " | ".join(f"{model:>{w}}" for model, w in zip(models, col_widths[1:]))
                print(header)
                print("-" * len(header))
                
                for idx, row in pivot_df.iterrows():
                    row_str = f"{idx:<{col_widths[0]}} | "
                    max_val = row.max()
                    vals_str = []
                    for model, w in zip(models, col_widths[1:]):
                        val = row[model]
                        if pd.isna(val):
                            val_str = f"{'N/A':>{w}}"
                        else:
                            val_str_raw = f"{val:>{w}.2f}"
                            if val == max_val and val > 0:
                                val_str = f"{GREEN}{val_str_raw}{RESET}"
                            else:
                                val_str = val_str_raw
                        vals_str.append(val_str)
                    row_str += " | ".join(vals_str)
                    print(row_str)

if __name__ == "__main__":
    main()

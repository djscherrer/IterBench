import os
import argparse
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
        
    # The peak sustained throughput is the RPS from the valid step with the highest User Count
    best_step = valid_steps.loc[valid_steps['User Count'].idxmax()]
    
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
                try:
                    idx = parts.index("results_snapshots")
                    model = parts[idx+2]
                    scenario = parts[idx+3]
                    env = parts[idx+4]
                except ValueError:
                    # Fallback
                    model = parts[-7] if len(parts) >= 7 else "unknown"
                    scenario = parts[-6] if len(parts) >= 6 else "unknown"
                    env = parts[-5] if len(parts) >= 5 else "unknown"

                summary.append({
                    'Model': model,
                    'Scenario': scenario,
                    'Environment': env,
                    'Peak Sustained RPS': round(peak_rps, 2),
                    'Path': str(f.parent)
                })
                
        if summary:
            summary_df = pd.DataFrame(summary)
            # Sort for readability
            summary_df = summary_df.sort_values(by=['Scenario', 'Environment', 'Model'])
            
            print("--- Consolidated Summary ---")
            print(summary_df[['Scenario', 'Environment', 'Model', 'Peak Sustained RPS']].to_string(index=False))
            
            out_file = "throughput_summary.csv"
            summary_df.to_csv(out_file, index=False)
            print(f"\n==> Saved full consolidated summary to {out_file}")
            
            # Print averaged table
            avg_df = summary_df.groupby(['Scenario', 'Environment', 'Model'])['Peak Sustained RPS'].mean().reset_index()
            avg_df.rename(columns={'Peak Sustained RPS': 'Avg Peak Sustained RPS'}, inplace=True)
            avg_df['Avg Peak Sustained RPS'] = avg_df['Avg Peak Sustained RPS'].round(2)
            
            avg_out_file = "throughput_summary_averaged.csv"
            avg_df.to_csv(avg_out_file, index=False)
            print(f"\n==> Saved averaged summary to {avg_out_file}")

            print("\n--- Average Peak Sustained RPS by Configuration (Averaged across samples) ---")
            
            avg_df['Scenario-Environment'] = avg_df['Scenario'] + '-' + avg_df['Environment']
            pivot_df = avg_df.pivot(index='Scenario-Environment', columns='Model', values='Avg Peak Sustained RPS')
            
            GREEN = '\033[92m'
            RESET = '\033[0m'
            
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

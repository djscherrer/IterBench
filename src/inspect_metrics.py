import argparse
import csv
import json
import os
import pathlib
import sys
from typing import Optional


def find_latest_run_dir(base_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Find the most recent perf-* directory in the base directory or its parent."""
    search_dirs = [base_dir, base_dir.parent]
    
    candidates = []
    for d in search_dirs:
        if not d.exists() or not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir() and child.name.startswith("perf-"):
                candidates.append(child)
                
    if not candidates:
        return None
        
    # Sort by name (which includes timestamp like 20260501-115415)
    candidates.sort(key=lambda p: p.name)
    return candidates[-1]


def get_host_roles(run_dir: pathlib.Path) -> dict[str, set[str]]:
    """Derive host roles from config.json, matching logic in plot_remote_perf.py."""
    cfg_path = run_dir / "config.json"
    if not cfg_path.is_file():
        return {}
        
    try:
        with open(cfg_path, "r") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
        
    erc = cfg.get("effective_remote_config") or {}
    if not isinstance(erc, dict):
        return {}
        
    stats_root = run_dir / "stats"
    hosts = []
    if stats_root.exists():
        hosts = sorted([p.name for p in stats_root.iterdir() if p.is_dir()])
        
    roles: dict[str, set[str]] = {h: set() for h in hosts}
    
    def host_slug(h: str) -> str:
        s = str(h).split("@")[-1].split(":")[0]
        return s.split(".")[0]
        
    def _add_roles(tag: str, host_list) -> None:
        if not host_list:
            return
        for h in host_list:
            slug = host_slug(str(h))
            if not slug:
                continue
            roles.setdefault(slug, set()).add(tag)
            
    load_master = erc.get("load_master")
    load_workers = erc.get("load_workers") or []
    load_all = []
    if load_master is not None and str(load_master).strip():
        load_all.append(load_master)
    if isinstance(load_workers, list):
        load_all.extend(load_workers)
        
    _add_roles("CL", load_all)
    _add_roles("BE", erc.get("backend_hosts"))
    
    lb_host = erc.get("lb_host")
    if lb_host is not None and str(lb_host).strip():
        roles.setdefault(host_slug(str(lb_host)), set()).add("LB")
        
    db_hosts = erc.get("db_hosts") or []
    if isinstance(db_hosts, list) and db_hosts:
        roles.setdefault(host_slug(str(db_hosts[0])), set()).add("DB")
        
    for h in hosts:
        if not roles.get(h):
            roles[h].add("BE")
            
    return roles


def cmd_summary(run_dir: pathlib.Path) -> None:
    stats_candidates = sorted(run_dir.glob("bench_results_*_stats.csv"))
    if not stats_candidates:
        print(f"Error: No bench_results_*_stats.csv found in {run_dir}")
        return
        
    stats_path = stats_candidates[0]
    agg_row = None
    
    try:
        with open(stats_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Name") == "Aggregated":
                    agg_row = row
                    break
    except Exception as e:
        print(f"Error reading {stats_path}: {e}")
        return
        
    if not agg_row:
        print(f"Error: Could not find 'Aggregated' row in {stats_path}")
        return
        
    print(f"RUN SUMMARY: {run_dir.name}")
    print("-" * 40)
    
    rps = float(agg_row.get("Requests/s", 0))
    fails_per_sec = float(agg_row.get("Failures/s", 0))
    req_count = int(agg_row.get("Request Count", 0))
    fail_count = int(agg_row.get("Failure Count", 0))
    
    err_rate = (fail_count / req_count * 100) if req_count > 0 else 0.0
    
    p50 = agg_row.get("50%", "N/A")
    p99 = agg_row.get("99%", "N/A")
    
    print(f"Requests/s:  {rps:.1f}")
    print(f"Failures/s:  {fails_per_sec:.1f} ({err_rate:.1f}% Error Rate)")
    print(f"Latency P50: {p50} ms")
    print(f"Latency P99: {p99} ms")


def cmd_resources(run_dir: pathlib.Path) -> None:
    stats_root = run_dir / "stats"
    if not stats_root.exists() or not stats_root.is_dir():
        print(f"Error: No stats directory found in {run_dir}")
        return
        
    roles = get_host_roles(run_dir)
    
    print(f"RESOURCE UTILIZATION (Peak % during run): {run_dir.name}")
    print(f"{'Host':<15} {'Role':<6} {'CPU%':<8} {'MEM%':<8} {'DiskWait%':<10} {'Notes'}")
    print("-" * 65)
    
    for host_dir in sorted(stats_root.iterdir()):
        if not host_dir.is_dir():
            continue
            
        host = host_dir.name
        hp_path = host_dir / "host_performance.csv"
        
        if not hp_path.exists():
            continue
            
        peak_cpu = 0.0
        peak_mem = 0.0
        peak_iowait = 0.0
        
        try:
            with open(hp_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cpu_ratio = float(row.get("cpu_usage_ratio", 0)) if row.get("cpu_usage_ratio") else 0
                    mem_pct = float(row.get("mem_used_pct", 0)) if row.get("mem_used_pct") else 0
                    io_wait = float(row.get("cpu_iowait_ratio", 0)) if row.get("cpu_iowait_ratio") else 0
                    
                    # Some files might have container_cpu_pct instead
                    container_cpu = row.get("container_cpu_pct")
                    if container_cpu and str(container_cpu).strip():
                        cpu_pct = float(container_cpu)
                    else:
                        cpu_pct = cpu_ratio * 100.0
                        
                    peak_cpu = max(peak_cpu, cpu_pct)
                    peak_mem = max(peak_mem, mem_pct)
                    peak_iowait = max(peak_iowait, io_wait * 100.0)
        except Exception as e:
            print(f"Error reading {hp_path}: {e}")
            continue
            
        host_role = "+".join(sorted(list(roles.get(host, set(["BE"])))))
        
        notes = []
        if peak_cpu > 90.0:
            notes.append("[CRITICAL] CPU Saturation")
        elif peak_cpu > 75.0:
            notes.append("[WARN] High CPU")
            
        if peak_mem > 90.0:
            notes.append("[WARN] High Memory")
            
        if peak_iowait > 10.0:
            notes.append("[WARN] High I/O Wait")
            
        if not notes:
            notes.append("[OK]")
            
        notes_str = ", ".join(notes)
        print(f"{host:<15} {host_role:<6} {peak_cpu:<8.1f} {peak_mem:<8.1f} {peak_iowait:<10.1f} {notes_str}")


def cmd_db(run_dir: pathlib.Path) -> None:
    stats_root = run_dir / "stats"
    if not stats_root.exists() or not stats_root.is_dir():
        print(f"Error: No stats directory found in {run_dir}")
        return
        
    roles = get_host_roles(run_dir)
    db_hosts = [h for h, r in roles.items() if "DB" in r]
    
    if not db_hosts:
        print("No DB hosts found in the run topology.")
        return
        
    print(f"DATABASE METRICS: {run_dir.name}")
    print("-" * 65)
    
    for host in db_hosts:
        host_dir = stats_root / host
        db_queue = host_dir / "db_queue.csv"
        db_perf = host_dir / "db_performance.csv"
        db_wait = host_dir / "db_wait_events.csv"
        
        peak_active = 0
        peak_locks = 0
        peak_total = 0
        
        if db_queue.exists():
            try:
                with open(db_queue, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        peak_active = max(peak_active, int(row.get("active_conns", 0) or 0))
                        peak_locks = max(peak_locks, int(row.get("lock_waiting_conns", 0) or 0))
                        peak_total = max(peak_total, int(row.get("total_conns", 0) or 0))
            except Exception as e:
                print(f"Error reading {db_queue}: {e}")
                    
        print(f"Host: {host}")
        print(f"  Peak Active Conns:  {peak_active} (Total connections: {peak_total})")
        print(f"  Peak Lock Waiting:  {peak_locks}")
        if peak_locks > 5:
            print("  [WARN] Significant lock contention detected!")
            
        if db_wait.exists():
            wait_counts = {}
            try:
                with open(db_wait, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        event = row.get("wait_event", "")
                        if event and event != "ClientRead" and event != "None":
                            wait_counts[event] = wait_counts.get(event, 0) + int(row.get("count", 0) or 0)
                if wait_counts:
                    top_wait = max(wait_counts.items(), key=lambda x: x[1])
                    print(f"  Top Wait Event:     {top_wait[0]} (Count: {top_wait[1]})")
            except Exception as e:
                print(f"Error reading {db_wait}: {e}")
                
        if db_perf.exists():
            total_time = 0.0
            total_calls = 0
            try:
                with open(db_perf, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # These are cumulative counters in db_performance
                        total_time = max(total_time, float(row.get("stmt_total_exec_time_ms", 0) or 0))
                        total_calls = max(total_calls, int(row.get("stmt_calls", 0) or 0))
                if total_calls > 0:
                    avg_time = total_time / total_calls
                    print(f"  Avg Statement Time: {avg_time:.2f} ms")
            except Exception as e:
                print(f"Error reading {db_perf}: {e}")
        print()


def cmd_endpoints(run_dir: pathlib.Path) -> None:
    stats_candidates = sorted(run_dir.glob("bench_results_*_stats.csv"))
    if not stats_candidates:
        print(f"Error: No bench_results_*_stats.csv found in {run_dir}")
        return
        
    stats_path = stats_candidates[0]
    
    print(f"ENDPOINT PERFORMANCE: {run_dir.name}")
    print(f"{'Name':<30} {'RPS':<8} {'P99(ms)':<8} {'Fail%'}")
    print("-" * 65)
    
    try:
        with open(stats_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("Name", "")
                if name == "Aggregated":
                    continue
                    
                rps = float(row.get("Requests/s", 0) or 0)
                req_count = int(row.get("Request Count", 0) or 0)
                fail_count = int(row.get("Failure Count", 0) or 0)
                err_rate = (fail_count / req_count * 100) if req_count > 0 else 0.0
                p99 = row.get("99%", "N/A")
                
                print(f"{name:<30} {rps:<8.1f} {p99:<8} {err_rate:.1f}%")
    except Exception as e:
        print(f"Error reading {stats_path}: {e}")


def cmd_network(run_dir: pathlib.Path) -> None:
    stats_root = run_dir / "stats"
    if not stats_root.exists() or not stats_root.is_dir():
        print(f"Error: No stats directory found in {run_dir}")
        return
        
    print(f"TCP LISTEN QUEUES (Peak Recv-Q): {run_dir.name}")
    print(f"{'Host':<15} {'Port':<8} {'Peak Recv-Q':<12} {'Notes'}")
    print("-" * 65)
    
    for host_dir in sorted(stats_root.iterdir()):
        if not host_dir.is_dir():
            continue
            
        host = host_dir.name
        sq_path = host_dir / "socket_queue.csv"
        
        if not sq_path.exists():
            continue
            
        peak_recv_q = {}
        
        try:
            with open(sq_path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    port = row.get("port")
                    if not port:
                        continue
                    rq = int(row.get("recv_q", 0) or 0)
                    peak_recv_q[port] = max(peak_recv_q.get(port, 0), rq)
        except Exception as e:
            print(f"Error reading {sq_path}: {e}")
            continue
            
        for port, peak in peak_recv_q.items():
            notes = "[WARN] Socket queue filling up! App is slow to accept()" if peak > 10 else "[OK]"
            print(f"{host:<15} {port:<8} {peak:<12} {notes}")



def main():
    parser = argparse.ArgumentParser(description="Performance Inspection Toolbox")
    parser.add_argument("command", choices=["summary", "resources", "db", "endpoints", "network", "all"], help="Inspection command to run")
    parser.add_argument("--run-dir", type=str, default=None, help="Path to the perf run directory (auto-detected if omitted)")
    
    args = parser.parse_args()
    
    cwd = pathlib.Path(os.getcwd())
    
    if args.run_dir:
        run_dir = pathlib.Path(args.run_dir)
    else:
        run_dir = find_latest_run_dir(cwd)
        
    if not run_dir or not run_dir.exists():
        print("Error: Could not find a perf run directory. Please specify --run-dir.", file=sys.stderr)
        sys.exit(1)
        
    if args.command == "summary":
        cmd_summary(run_dir)
    elif args.command == "resources":
        cmd_resources(run_dir)
    elif args.command == "db":
        cmd_db(run_dir)
    elif args.command == "endpoints":
        cmd_endpoints(run_dir)
    elif args.command == "network":
        cmd_network(run_dir)
    elif args.command == "all":
        cmd_summary(run_dir)
        print("\n" + "="*80 + "\n")
        cmd_resources(run_dir)
        print("\n" + "="*80 + "\n")
        cmd_db(run_dir)
        print("\n" + "="*80 + "\n")
        cmd_endpoints(run_dir)
        print("\n" + "="*80 + "\n")
        cmd_network(run_dir)

if __name__ == "__main__":
    main()

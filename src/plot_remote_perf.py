import json
import pathlib
import re
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from bench_models import host_slug


def _read_locust_stats_history(run_dir: pathlib.Path) -> pd.DataFrame:
    stats_candidates = sorted(run_dir.glob("bench_results_*_stats_history.csv"))
    if not stats_candidates:
        raise FileNotFoundError(
            f"No locust stats_history CSV found in {run_dir} "
            f"(expected bench_results_*_stats_history.csv)"
        )
    stats_path = stats_candidates[0]
    df = pd.read_csv(stats_path)
    df = df[df["Name"] == "Aggregated"].copy()
    if df.empty:
        raise ValueError(f"No Aggregated rows in {stats_path}")

    df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce")
    df["Requests/s"] = pd.to_numeric(df["Requests/s"], errors="coerce")
    df["Failures/s"] = pd.to_numeric(df["Failures/s"], errors="coerce")
    if "User Count" in df.columns:
        df["User Count"] = pd.to_numeric(df["User Count"], errors="coerce")

    df = df.dropna(subset=["Timestamp", "Requests/s", "Failures/s"])
    df = df.sort_values("Timestamp").reset_index(drop=True)
    df["t_s"] = df["Timestamp"] - df["Timestamp"].min()
    df["served_rps"] = df["Requests/s"]
    df["successful_rps"] = df["Requests/s"] - df["Failures/s"]
    df["failure_rps"] = df["Failures/s"]
    return df


def _iter_stats_files(run_dir: pathlib.Path, filename: str) -> Iterable[tuple[str, pathlib.Path]]:
    stats_root = run_dir / "stats"
    if not stats_root.exists():
        return []
    out: list[tuple[str, pathlib.Path]] = []
    for p in sorted(stats_root.glob(f"*/{filename}")):
        host = p.parent.name
        out.append((host, p))
    return out


def _read_csv_nonempty(path: pathlib.Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    return df


def _cpuset_cpu_count(cpuset: str) -> int | None:
    """Count logical CPUs listed in a cpuset string (e.g. '0-3', '0,2,4-5')."""
    s = (cpuset or "").strip()
    if not s:
        return None
    total = 0
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a.strip()), int(b.strip())
            except ValueError:
                return None
            if hi < lo:
                return None
            total += hi - lo + 1
        else:
            try:
                int(part.strip())
            except ValueError:
                return None
            total += 1
    return total if total > 0 else None


def _docker_cpu_saturation_pct_by_stats_host(run_dir: pathlib.Path) -> dict[str, float]:
    """
    Map stats/ subdirectory name -> Docker CPUPerc value at full CPU quota.

    Uses resolved_system_topology in config.json: min(--cpus, cpuset size) when both are set.
    """
    cfg_path = run_dir / "config.json"
    if not cfg_path.is_file():
        return {}

    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    topo = cfg.get("resolved_system_topology") or {}
    erc = cfg.get("effective_remote_config") or {}

    def _cap_perc(res: dict) -> float | None:
        caps: list[float] = []
        c = res.get("cpus")
        if c is not None:
            caps.append(float(c) * 100.0)
        cs = res.get("cpuset_cpus")
        n = _cpuset_cpu_count(str(cs)) if cs else None
        if n is not None:
            caps.append(float(n) * 100.0)
        if not caps:
            return None
        return min(caps)

    be = _cap_perc(topo.get("backend_resources") or {})
    db = _cap_perc(topo.get("db_resources") or {})
    lb = _cap_perc(topo.get("lb_resources") or {})

    out: dict[str, float] = {}
    for h in erc.get("app_hosts") or []:
        slug = host_slug(str(h))
        if be is not None:
            out[slug] = be
    dh = erc.get("db_host")
    if dh and db is not None:
        out[host_slug(str(dh))] = db
    lh = erc.get("lb_host")
    if lh and lb is not None:
        out[host_slug(str(lh))] = lb
    return out


def _infer_host_roles(run_dir: pathlib.Path) -> dict[str, set[str]]:
    """
    Infer host roles from on-disk artifacts.

    Tags:
      - CL: client / load generator
      - LB: load balancer
      - BE: backend
      - DB: database
    """
    roles: dict[str, set[str]] = {}

    # Collect hosts present in stats/ (these are the machines we have telemetry for)
    stats_root = run_dir / "stats"
    hosts = []
    if stats_root.exists():
        hosts = sorted([p.name for p in stats_root.iterdir() if p.is_dir()])
    for h in hosts:
        roles[h] = set()

    # Client host: look for locust logs in bench.log ("r630-08/INFO/locust.main: Starting Locust ...")
    bench_log = run_dir / "bench.log"
    client_host: str | None = None
    if bench_log.exists():
        try:
            with open(bench_log, "r", errors="ignore") as f:
                for line in f:
                    m = re.search(r"\]\s+([^/\s]+)/INFO/locust\.main:\s+Starting Locust", line)
                    if m:
                        client_host = m.group(1).strip()
                        break
        except Exception:
            client_host = None

    if client_host:
        roles.setdefault(client_host, set()).add("CL")

    # DB host: any host whose socket_queue.csv includes port 5432
    db_host: str | None = None
    for host, sq_path in _iter_stats_files(run_dir, "socket_queue.csv"):
        df = _read_csv_nonempty(sq_path)
        if df is None or "port" not in df.columns:
            continue
        ports = pd.to_numeric(df["port"], errors="coerce").dropna().astype(int).unique().tolist()
        if 5432 in ports:
            db_host = host
            break
    if db_host:
        roles.setdefault(db_host, set()).add("DB")

    # ALB host: prefer a host that has port 80/443 in socket_queue; otherwise fall back to client_host.
    lb_host: str | None = None
    for host, sq_path in _iter_stats_files(run_dir, "socket_queue.csv"):
        df = _read_csv_nonempty(sq_path)
        if df is None or "port" not in df.columns:
            continue
        ports = pd.to_numeric(df["port"], errors="coerce").dropna().astype(int).unique().tolist()
        if 80 in ports or 443 in ports:
            lb_host = host
            break
    if lb_host is None:
        lb_host = client_host
    if lb_host:
        roles.setdefault(lb_host, set()).add("LB")

    # Everything else (with telemetry) is assumed to be backend
    for h in hosts:
        if "DB" in roles.get(h, set()):
            continue
        if "LB" in roles.get(h, set()):
            continue
        if "CL" in roles.get(h, set()):
            continue
        roles.setdefault(h, set()).add("BE")

    return roles


def _host_color(
    host_roles: dict[str, set[str]],
    host: str,
    *,
    backend_rank: int | None = None,
) -> tuple[float, float, float, float]:
    """
    Color scheme:
      - CL: blue
      - LB: violet (unless host also CL, then use CL blue)
      - DB: red
      - BE: green shades (different intensity by backend_rank)
    """
    roles = host_roles.get(host, set())
    if "CL" in roles:
        return (0.121, 0.466, 0.705, 1.0)  # tab:blue
    if "DB" in roles:
        return (0.839, 0.153, 0.157, 1.0)  # tab:red
    if "LB" in roles:
        return (0.58, 0.404, 0.741, 1.0)  # tab:purple-ish
    # backend
    cmap = plt.get_cmap("Greens")
    r = 0 if backend_rank is None else int(backend_rank)
    # Avoid extremes (too pale / too dark)
    t = 0.45 + (0.45 * ((r % 6) / 5.0))  # 0.45..0.9
    return cmap(t)


def _role_suffix(roles: dict[str, set[str]], host: str) -> str:
    tags = sorted(list(roles.get(host, set())))
    if not tags:
        return ""
    return " (" + "+".join(tags) + ")"


def _read_nginx_timing_csv(run_dir: pathlib.Path) -> pd.DataFrame | None:
    """
    Read Nginx timing access log (CSV-like) if present.

    Expected columns written by distributed_bench.py:
      msec,status,method,uri,request_time,upstream_response_time,upstream_connect_time,upstream_header_time

    Note: upstream_* fields can be '-' or a comma-separated list if multiple upstreams were tried.
    We take the first numeric value we can parse.
    """
    candidates = sorted((run_dir / "stats").glob("*/nginx_access_timing.csv"))
    if not candidates:
        return None
    path = candidates[0]
    try:
        df = pd.read_csv(
            path,
            header=None,
            names=[
                "msec",
                "status",
                "method",
                "uri",
                "request_time_s",
                "upstream_response_time_s",
                "upstream_connect_time_s",
                "upstream_header_time_s",
            ],
        )
    except Exception:
        return None
    if df.empty:
        return None

    def _first_num(x):
        if pd.isna(x):
            return np.nan
        s = str(x).strip()
        if not s or s == "-":
            return np.nan
        # nginx can emit "0.001, 0.002" if multiple upstreams; take first numeric token
        tok = s.split(",")[0].strip()
        try:
            return float(tok)
        except Exception:
            return np.nan

    df["msec"] = pd.to_numeric(df["msec"], errors="coerce")
    df["request_time_s"] = pd.to_numeric(df["request_time_s"], errors="coerce")
    df["upstream_response_time_s"] = df["upstream_response_time_s"].apply(_first_num)
    df["upstream_connect_time_s"] = df["upstream_connect_time_s"].apply(_first_num)
    df["upstream_header_time_s"] = df["upstream_header_time_s"].apply(_first_num)
    df = df.dropna(subset=["msec", "request_time_s"])
    if df.empty:
        return None

    df["ts_epoch_s"] = df["msec"].astype(float)
    df["t_s"] = df["ts_epoch_s"] - float(df["ts_epoch_s"].min())
    df["nginx_total_ms"] = df["request_time_s"].astype(float) * 1000.0
    df["upstream_ms"] = df["upstream_response_time_s"].astype(float) * 1000.0
    return df


def _read_db_performance_csv(run_dir: pathlib.Path) -> pd.DataFrame | None:
    """
    Read db_performance.csv from stats/* if present and compute avg statement time series.
    """
    candidates = sorted((run_dir / "stats").glob("*/db_performance.csv"))
    if not candidates:
        return None
    db_path = candidates[0]
    try:
        db = pd.read_csv(db_path)
    except Exception:
        return None
    if db.empty:
        return None
    if not {"ts", "stmt_calls", "stmt_total_exec_time_ms"}.issubset(set(db.columns)):
        return None
    db["ts"] = pd.to_numeric(db["ts"], errors="coerce")
    db["stmt_calls"] = pd.to_numeric(db["stmt_calls"], errors="coerce")
    db["stmt_total_exec_time_ms"] = pd.to_numeric(db["stmt_total_exec_time_ms"], errors="coerce")
    db = db.dropna(subset=["ts", "stmt_calls", "stmt_total_exec_time_ms"]).sort_values("ts")
    if db.empty:
        return None
    db["ts"] = db["ts"].astype(float)
    db["t_s"] = db["ts"] - float(db["ts"].min())
    db["d_calls"] = db["stmt_calls"].diff()
    db["d_exec_ms"] = db["stmt_total_exec_time_ms"].diff()
    db["avg_db_stmt_ms"] = np.where(db["d_calls"] > 0, db["d_exec_ms"] / db["d_calls"], np.nan)
    db.loc[(db["avg_db_stmt_ms"] < 0) | np.isinf(db["avg_db_stmt_ms"]), "avg_db_stmt_ms"] = np.nan
    return db


def plot_remote_perf_for_run_dir(
    run_dir: pathlib.Path,
    out_dir: pathlib.Path | None = None,
    *,
    rolling_window: int = 5,
) -> list[pathlib.Path]:
    """
    Plot remote performance metrics for a single perf run directory.

    Expected in run_dir:
      - bench_results_*_stats_history.csv
      - stats/<host>/host_performance.csv (optional, but recommended)
      - stats/<host>/socket_queue.csv (optional)
      - stats/<host>/db_queue.csv (optional)

    Produces plots into out_dir (default: run_dir/plots_remote_perf).
    """
    run_dir = pathlib.Path(run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist or is not a directory: {run_dir}")

    out_dir = out_dir or (run_dir / "plots_remote_perf")
    out_dir.mkdir(parents=True, exist_ok=True)

    df_locust = _read_locust_stats_history(run_dir)
    rw = max(1, int(rolling_window))
    df_locust["successful_rps_smooth"] = (
        df_locust["successful_rps"].rolling(window=rw, min_periods=1).mean()
    )

    host_roles = _infer_host_roles(run_dir)

    outputs: list[pathlib.Path] = []

    # Note: we intentionally do NOT generate throughput-over-time here.
    # The main plotter already creates run_dir/plots/throughput_over_time.png.

    # 2) Combined CPU/MEM/iowait vs achieved RPS, aligned by ts_epoch_s ~ Timestamp
    # Locust "Timestamp" is epoch seconds in this run's CSV.
    df_locust_align = df_locust.rename(columns={"Timestamp": "ts_epoch_s"}).copy()
    # Ensure merge_asof keys have identical dtype (float) across sources.
    df_locust_align["ts_epoch_s"] = pd.to_numeric(
        df_locust_align["ts_epoch_s"], errors="coerce"
    ).astype("float64")
    df_locust_align = df_locust_align.dropna(subset=["ts_epoch_s"]).sort_values("ts_epoch_s")

    merged_by_host: dict[str, pd.DataFrame] = {}
    for host, hp_path in _iter_stats_files(run_dir, "host_performance.csv"):
        df_hp = _read_csv_nonempty(hp_path)
        if df_hp is None:
            continue
        if "ts_epoch_s" not in df_hp.columns:
            continue

        df_hp = df_hp.copy()
        df_hp["ts_epoch_s"] = pd.to_numeric(df_hp["ts_epoch_s"], errors="coerce").astype(
            "float64"
        )
        df_hp = df_hp.dropna(subset=["ts_epoch_s"]).sort_values("ts_epoch_s")
        if df_hp.empty:
            continue

        merged = pd.merge_asof(
            df_hp,
            df_locust_align[["ts_epoch_s", "successful_rps_smooth"]],
            on="ts_epoch_s",
            direction="nearest",
            tolerance=2.0,  # seconds; host stats interval is ~15s, locust is 1s
        ).dropna(subset=["successful_rps_smooth"])
        if merged.empty:
            continue

        merged_by_host[host] = merged

    # Combined scatter plots: one plot per metric, all hosts together
    hosts_sorted = sorted(merged_by_host.keys())
    backend_hosts_sorted = sorted(
        [h for h in hosts_sorted if "BE" in host_roles.get(h, set())]
    )
    backend_rank = {h: i for i, h in enumerate(backend_hosts_sorted)}

    # Time alignment reference (epoch seconds)
    locust_t0 = float(df_locust_align["ts_epoch_s"].min()) if not df_locust_align.empty else None
    saturation_by_host = _docker_cpu_saturation_pct_by_stats_host(run_dir)

    def _plot_host_metric_over_time(
        *,
        metric_col: str,
        metric_label: str,
        metric_transform,
        out_name: str,
        title: str,
    ) -> None:
        if locust_t0 is None:
            return
        fig, ax = plt.subplots(figsize=(12, 7))
        # Overlay successful RPS (smoothed) once
        ax_rps = ax.twinx()
        ax_rps.plot(
            df_locust["t_s"].to_numpy(),
            df_locust["successful_rps_smooth"].to_numpy(),
            color="#111111",
            linewidth=1.8,
            alpha=0.55,
            label="Successful req/s (smoothed)",
        )
        ax_rps.set_ylabel("Successful req/s")

        for host in hosts_sorted:
            dfm = merged_by_host[host]
            if metric_col not in dfm.columns or "ts_epoch_s" not in dfm.columns:
                continue
            x = (dfm["ts_epoch_s"].to_numpy(dtype=float) - locust_t0)
            y_raw = dfm[metric_col].to_numpy(dtype=float)
            y = metric_transform(y_raw)
            c = _host_color(host_roles, host, backend_rank=backend_rank.get(host))
            label = f"{host}{_role_suffix(host_roles, host)}"
            ax.plot(x, y, linewidth=2.0, alpha=0.9, color=c, label=label)

        ax.set_xlabel("Time (s)")
        ax.set_ylabel(metric_label)
        ax.set_title(f"{title}\n{run_dir.name}")
        ax.grid(alpha=0.25, linestyle="--")
        # Merge legends
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax_rps.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="best", fontsize=9)

        out_path = out_dir / out_name
        fig.tight_layout()
        fig.savefig(out_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
        outputs.append(out_path)

    def _plot_cpu_busy_over_time() -> None:
        """Prefer `container_cpu_pct` (docker stats); else host-wide cpu_usage_ratio. Dashed = topology CPU cap."""
        if locust_t0 is None:
            return
        fig, ax = plt.subplots(figsize=(12, 7))
        ax_rps = ax.twinx()
        ax_rps.plot(
            df_locust["t_s"].to_numpy(),
            df_locust["successful_rps_smooth"].to_numpy(),
            color="#111111",
            linewidth=1.8,
            alpha=0.55,
            label="Successful req/s (smoothed)",
        )
        ax_rps.set_ylabel("Successful req/s")

        any_container = False
        for host in hosts_sorted:
            dfm = merged_by_host[host]
            if "ts_epoch_s" not in dfm.columns:
                continue
            x = dfm["ts_epoch_s"].to_numpy(dtype=float) - locust_t0
            c = _host_color(host_roles, host, backend_rank=backend_rank.get(host))
            label = f"{host}{_role_suffix(host_roles, host)}"

            use_container = False
            y: np.ndarray
            if "container_cpu_pct" in dfm.columns:
                y_cont = pd.to_numeric(dfm["container_cpu_pct"], errors="coerce").to_numpy(
                    dtype=float
                )
                use_container = bool(np.isfinite(y_cont).any())
            else:
                y_cont = None

            if use_container and y_cont is not None:
                any_container = True
                y = y_cont
            else:
                if "cpu_usage_ratio" not in dfm.columns:
                    continue
                y = dfm["cpu_usage_ratio"].to_numpy(dtype=float) * 100.0

            ax.plot(x, y, linewidth=2.0, alpha=0.9, color=c, label=label)

            if use_container and host in saturation_by_host:
                cap = saturation_by_host[host]
                ax.axhline(
                    y=cap,
                    xmin=0.0,
                    xmax=1.0,
                    color=c,
                    linestyle="--",
                    linewidth=1.25,
                    alpha=0.8,
                    zorder=1,
                )

        if any_container:
            metric_label = "CPU % (Docker CPUPerc: 100% ≈ one core; dashed = configured cap)"
            title = (
                "Container CPU (docker stats) over time where available; "
                "dashed line = CPU ceiling from topology (--cpus / cpuset)"
            )
        else:
            metric_label = "Host non-idle CPU (% of all logical CPUs)"
            title = (
                "Host CPU busy over time (machine-wide /proc/stat; no container_cpu_pct in CSV) "
                "+ successful RPS"
            )

        ax.set_xlabel("Time (s)")
        ax.set_ylabel(metric_label)
        ax.set_title(f"{title}\n{run_dir.name}")
        ax.grid(alpha=0.25, linestyle="--")
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax_rps.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="best", fontsize=9)

        out_path = out_dir / "cpu_busy_over_time__all_hosts.png"
        fig.tight_layout()
        fig.savefig(out_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
        outputs.append(out_path)

    # Time-series plots (preferred for understanding dynamics)
    _plot_cpu_busy_over_time()
    _plot_host_metric_over_time(
        metric_col="cpu_usage_ratio",
        metric_label="Host non-idle CPU (% of all logical CPUs)",
        metric_transform=lambda v: v * 100.0,
        out_name="cpu_host_machine_wide_over_time__all_hosts.png",
        title=(
            "Host CPU busy over time (machine-wide /proc/stat; not per-container) "
            "+ successful RPS"
        ),
    )
    _plot_host_metric_over_time(
        metric_col="cpu_iowait_ratio",
        metric_label="CPU iowait (%)",
        metric_transform=lambda v: v * 100.0,
        out_name="cpu_iowait_over_time__all_hosts.png",
        title="CPU iowait over time (all hosts) + successful RPS",
    )
    _plot_host_metric_over_time(
        metric_col="mem_used_pct",
        metric_label="Memory used (%)",
        metric_transform=lambda v: v,
        out_name="mem_used_pct_over_time__all_hosts.png",
        title="Memory used over time (all hosts) + successful RPS",
    )

    def _annotate_last(ax: plt.Axes, x: np.ndarray, y: np.ndarray, label: str, color):
        if len(x) == 0:
            return
        ax.annotate(
            label,
            xy=(x[-1], y[-1]),
            xytext=(6, 6),
            textcoords="offset points",
            fontsize=8,
            color=color,
            alpha=0.9,
        )

    # We intentionally do NOT generate "metric vs RPS" scatter plots anymore.
    # The over-time plots (with RPS overlay) are easier to interpret.

    # 3) Socket listen Recv-Q vs time: one plot per port, all hosts together
    socket_by_host: dict[str, pd.DataFrame] = {}
    all_ports: set[int] = set()
    for host, sq_path in _iter_stats_files(run_dir, "socket_queue.csv"):
        df_sq = _read_csv_nonempty(sq_path)
        if df_sq is None:
            continue
        if not {"ts_epoch_s", "port", "recv_q"}.issubset(set(df_sq.columns)):
            continue
        df_sq = df_sq.copy()
        df_sq["ts_epoch_s"] = pd.to_numeric(df_sq["ts_epoch_s"], errors="coerce")
        df_sq["recv_q"] = pd.to_numeric(df_sq["recv_q"], errors="coerce")
        df_sq["port"] = pd.to_numeric(df_sq["port"], errors="coerce")
        df_sq = df_sq.dropna(subset=["ts_epoch_s", "recv_q", "port"]).sort_values("ts_epoch_s")
        if df_sq.empty:
            continue
        df_sq["port"] = df_sq["port"].astype(int)
        socket_by_host[host] = df_sq
        all_ports.update(df_sq["port"].unique().tolist())

    for port in sorted(all_ports):
        fig, ax = plt.subplots(figsize=(12, 7))
        # Align all hosts for this port to the same t0 so traces are comparable.
        port_min_ts = None
        for host in sorted(socket_by_host.keys()):
            df_sq = socket_by_host[host]
            df_p = df_sq[df_sq["port"] == port]
            if df_p.empty:
                continue
            ts0 = float(pd.to_numeric(df_p["ts_epoch_s"], errors="coerce").dropna().min())
            if port_min_ts is None or ts0 < port_min_ts:
                port_min_ts = ts0
        if port_min_ts is None:
            plt.close(fig)
            continue

        for host in sorted(socket_by_host.keys()):
            df_sq = socket_by_host[host]
            df_p = df_sq[df_sq["port"] == port].sort_values("ts_epoch_s")
            if df_p.empty:
                continue
            x = (df_p["ts_epoch_s"] - port_min_ts).to_numpy()
            y = df_p["recv_q"].to_numpy()
            c = _host_color(host_roles, host, backend_rank=backend_rank.get(host))
            label = f"{host}{_role_suffix(host_roles, host)}"
            ax.plot(x, y, linewidth=2.0, color=c, alpha=0.9, label=label)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("LISTEN Recv-Q (connections ready-to-accept)")
        ax.set_title(f"TCP listen Recv-Q over time (port {port})\n{run_dir.name}")
        ax.grid(alpha=0.25, linestyle="--")
        ax.set_ylim(bottom=0)
        ax.legend(loc="best", fontsize=9)
        out_path = out_dir / f"listen_recvq_over_time__port_{port}__all_hosts.png"
        fig.tight_layout()
        fig.savefig(out_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
        outputs.append(out_path)

    # 4) DB queue metrics over time (optional; may be empty)
    for host, dbq_path in _iter_stats_files(run_dir, "db_queue.csv"):
        df_dbq = _read_csv_nonempty(dbq_path)
        if df_dbq is None:
            continue
        if "ts" not in df_dbq.columns:
            continue
        # Older capture used wall-clock strings; plot as index order if we can't parse.
        df_dbq = df_dbq.copy()
        for c in [
            "total_conns",
            "active_conns",
            "waiting_conns",
            "lock_waiting_conns",
            "idle_in_tx_conns",
        ]:
            if c in df_dbq.columns:
                df_dbq[c] = pd.to_numeric(df_dbq[c], errors="coerce")
        cols = [c for c in df_dbq.columns if c.endswith("_conns")]
        if not cols:
            continue
        df_dbq = df_dbq.dropna(subset=cols, how="all")
        if df_dbq.empty:
            continue

        fig, ax = plt.subplots(figsize=(12, 6))
        x = range(len(df_dbq))
        for c in cols:
            if df_dbq[c].notna().any():
                ax.plot(list(x), df_dbq[c], linewidth=2.0, label=c)
        ax.set_xlabel("Sample index")
        ax.set_ylabel("Connections")
        ax.set_title(f"{host}: Postgres session/queue metrics (order as captured)\n{run_dir.name}")
        ax.grid(alpha=0.25, linestyle="--")
        ax.legend(loc="upper right")
        out_path = out_dir / f"{host}__db_queue_over_time.png"
        fig.tight_layout()
        fig.savefig(out_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
        outputs.append(out_path)

    # 5) Latency attribution over time (best-effort)
    # We can combine:
    # - Total latency (Locust p95/p99) over time
    # - Nginx total request_time and upstream_response_time (from timing access log)
    # - DB avg statement time (from db_performance counters; an approximation, not per-request)
    try:
        df_ng = _read_nginx_timing_csv(run_dir)
        df_db = _read_db_performance_csv(run_dir)
        stats_candidates = sorted(run_dir.glob("bench_results_*_stats_history.csv"))
        if stats_candidates:
            loc = pd.read_csv(stats_candidates[0])
            loc = loc[loc["Name"] == "Aggregated"].copy()
            loc["Timestamp"] = pd.to_numeric(loc["Timestamp"], errors="coerce")
            loc["95%"] = pd.to_numeric(loc["95%"], errors="coerce")
            loc["99%"] = pd.to_numeric(loc["99%"], errors="coerce")
            loc = loc.dropna(subset=["Timestamp", "95%", "99%"]).sort_values("Timestamp")
            if not loc.empty:
                loc["t_s"] = loc["Timestamp"] - float(loc["Timestamp"].min())

                fig, ax = plt.subplots(figsize=(12, 7))

                # Stacked components: DB avg stmt (approx) + upstream minus DB (clipped)
                if df_ng is not None and not df_ng.empty:
                    # Bucket nginx timings into 1s bins
                    df_ng = df_ng.copy()
                    df_ng["t_bin"] = df_ng["t_s"].astype(int)
                    ng_agg = df_ng.groupby("t_bin").agg(
                        nginx_total_ms=("nginx_total_ms", "mean"),
                        upstream_ms=("upstream_ms", "mean"),
                    )
                    t = ng_agg.index.to_numpy(dtype=float)
                    upstream_ms = ng_agg["upstream_ms"].to_numpy(dtype=float)

                    db_ms = None
                    if df_db is not None and not df_db.empty and df_db["avg_db_stmt_ms"].notna().any():
                        df_db2 = df_db.dropna(subset=["avg_db_stmt_ms"]).copy()
                        df_db2["t_bin"] = df_db2["t_s"].astype(int)
                        db_agg = df_db2.groupby("t_bin").agg(db_ms=("avg_db_stmt_ms", "mean"))
                        # align to nginx bins
                        db_ms = db_agg.reindex(ng_agg.index).interpolate(limit_direction="both")["db_ms"].to_numpy(
                            dtype=float
                        )
                    if db_ms is None:
                        db_ms = np.zeros_like(upstream_ms)

                    backend_non_db_ms = np.maximum(0.0, upstream_ms - db_ms)

                    ax.stackplot(
                        t,
                        db_ms,
                        backend_non_db_ms,
                        labels=["DB avg stmt (approx)", "Upstream minus DB (approx)"],
                        colors=["#d62728", "#2ca02c"],
                        alpha=0.25,
                    )
                    ax.plot(t, upstream_ms, color="#2ca02c", linewidth=1.6, alpha=0.85, label="Upstream (mean)")
                    ax.plot(
                        t,
                        ng_agg["nginx_total_ms"].to_numpy(dtype=float),
                        color="#9467bd",
                        linewidth=1.6,
                        alpha=0.75,
                        label="Nginx total (mean)",
                    )

                # Locust total latency overlay (p95/p99)
                ax.plot(
                    loc["t_s"].to_numpy(),
                    loc["95%"].to_numpy(),
                    color="#1f77b4",
                    linewidth=2.0,
                    label="Locust p95 (ms)",
                )
                ax.plot(
                    loc["t_s"].to_numpy(),
                    loc["99%"].to_numpy(),
                    color="#1f77b4",
                    linewidth=1.6,
                    linestyle="--",
                    alpha=0.9,
                    label="Locust p99 (ms)",
                )

                ax.set_xlabel("Time (s)")
                ax.set_ylabel("Latency (ms)")
                ax.set_title(f"Latency attribution over time (best-effort)\n{run_dir.name}")
                ax.grid(alpha=0.25, linestyle="--")
                ax.legend(loc="best", fontsize=9)
                # Put system-level attribution next to other run-level plots.
                general_out_dir = run_dir / "plots"
                general_out_dir.mkdir(parents=True, exist_ok=True)
                out_path = general_out_dir / "latency_attribution_over_time.png"
                fig.tight_layout()
                fig.savefig(out_path, dpi=300, bbox_inches="tight")
                plt.close(fig)
                outputs.append(out_path)
    except Exception:
        # Best-effort: do not fail plotting for missing/partial inputs
        pass

    return outputs


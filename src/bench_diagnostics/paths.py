"""
On-disk layout for bench diagnostics (mode-specific subtrees).

Only the subtree for the active :class:`DiagnosticsMode` is created — a
kubernetes run never materialises ``diagnostics/distributed/``, and vice
versa. Load-generator host metrics always live under ``diagnostics/hosts/``
(both modes use remote Locust on SSH machines).

Layout::

    <run_dir>/diagnostics/
    ├── hosts/                           # shared: Locust load-generator SSH hosts
    │   └── <host_slug>/host_performance.csv
    ├── kubernetes/                      # DiagnosticsMode.KUBERNETES only
    │   ├── cluster/
    │   │   ├── kubectl_top_pods.csv
    │   │   ├── kubectl_top_nodes.csv
    │   │   ├── pod_status.csv
    │   │   ├── events.jsonl
    │   │   └── restart_logs/
    │   ├── pods/
    │   │   ├── backend.log
    │   │   └── postgres.log
    │   └── database/
    │       ├── pg_stat_activity.csv
    │       └── pg_stat_database.csv
    └── distributed/                     # DiagnosticsMode.DISTRIBUTED only
        ├── hosts/
        │   └── <host_slug>/
        │       ├── host_performance.csv
        │       ├── socket_queue.csv
        │       └── db_performance.csv
        └── database/
            └── db_performance.csv       # local docker bench postgres sampler
"""

from __future__ import annotations

from pathlib import Path


def diagnostics_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/`` root (created on first write)."""
    d = run_dir / "diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Shared load-host metrics (both modes) ------------------------------------


def load_host_dir(run_dir: Path, host_slug: str) -> Path:
    """``<run_dir>/diagnostics/hosts/<host_slug>/`` for a Locust SSH host."""
    d = diagnostics_dir(run_dir) / "hosts" / host_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Kubernetes mode only ---------------------------------------------------


def kubernetes_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/``."""
    d = diagnostics_dir(run_dir) / "kubernetes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_cluster_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/cluster/``."""
    d = kubernetes_dir(run_dir) / "cluster"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_pods_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/pods/``."""
    d = kubernetes_dir(run_dir) / "pods"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_database_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/database/``."""
    d = kubernetes_dir(run_dir) / "database"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Distributed mode only --------------------------------------------------


def distributed_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/distributed/``."""
    d = diagnostics_dir(run_dir) / "distributed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def distributed_host_dir(run_dir: Path, host_slug: str) -> Path:
    """``<run_dir>/diagnostics/distributed/hosts/<host_slug>/`` for workload hosts."""
    d = distributed_dir(run_dir) / "hosts" / host_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def distributed_database_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/distributed/database/`` (e.g. local docker postgres)."""
    d = distributed_dir(run_dir) / "database"
    d.mkdir(parents=True, exist_ok=True)
    return d

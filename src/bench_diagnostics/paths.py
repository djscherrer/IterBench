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
    │   ├── logs/
    │   │   ├── backend.log
    │   │   ├── postgres.log
    │   │   ├── postgres-replica.log
    │   │   ├── pgbouncer.log
    │   │   ├── pgbouncer-read.log
    │   │   ├── redis.log
    │   │   └── restarts/
    │   └── metrics/
    │       ├── cluster/
    │       │   ├── kubectl_top_pods.csv
    │       │   ├── kubectl_top_nodes.csv
    │       │   ├── pod_status.csv
    │       │   └── events.jsonl
    │       ├── database/
    │       ├── pooler/
    │       └── cache/
    └── distributed/                     # DiagnosticsMode.DISTRIBUTED only
        ├── hosts/
        └── database/
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


def kubernetes_logs_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/logs/`` (pod log streams)."""
    d = kubernetes_dir(run_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_logs_restarts_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/logs/restarts/``."""
    d = kubernetes_logs_dir(run_dir) / "restarts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_metrics_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/metrics/``."""
    d = kubernetes_dir(run_dir) / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_metrics_cluster_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/metrics/cluster/``."""
    d = kubernetes_metrics_dir(run_dir) / "cluster"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_metrics_database_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/metrics/database/``."""
    d = kubernetes_metrics_dir(run_dir) / "database"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_metrics_pooler_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/metrics/pooler/``."""
    d = kubernetes_metrics_dir(run_dir) / "pooler"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kubernetes_metrics_cache_dir(run_dir: Path) -> Path:
    """``<run_dir>/diagnostics/kubernetes/metrics/cache/``."""
    d = kubernetes_metrics_dir(run_dir) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Read helpers (new layout, then legacy fallbacks) -------------------------


def resolve_kubernetes_logs_dir(run_dir: Path) -> Path:
    """Logs directory for reading (``logs/`` or legacy ``pods/``)."""
    new = kubernetes_dir(run_dir) / "logs"
    legacy = kubernetes_dir(run_dir) / "pods"
    if new.is_dir() and any(new.iterdir()):
        return new
    if legacy.is_dir():
        return legacy
    return new


def resolve_kubernetes_logs_restarts_dir(run_dir: Path) -> Path:
    new = kubernetes_logs_restarts_dir(run_dir)
    legacy = kubernetes_dir(run_dir) / "cluster" / "restart_logs"
    if new.is_dir() and any(new.iterdir()):
        return new
    if legacy.is_dir():
        return legacy
    return new


def resolve_kubernetes_metrics_cluster_dir(run_dir: Path) -> Path:
    new = kubernetes_metrics_cluster_dir(run_dir)
    legacy = kubernetes_dir(run_dir) / "cluster"
    if new.is_dir() and any(new.iterdir()):
        return new
    if legacy.is_dir():
        return legacy
    return new


def resolve_kubernetes_metrics_database_dir(run_dir: Path) -> Path:
    new = kubernetes_metrics_database_dir(run_dir)
    legacy = kubernetes_dir(run_dir) / "database"
    if new.is_dir() and any(new.iterdir()):
        return new
    if legacy.is_dir():
        return legacy
    return new


def resolve_kubernetes_metrics_pooler_dir(run_dir: Path) -> Path:
    new = kubernetes_metrics_pooler_dir(run_dir)
    legacy = kubernetes_dir(run_dir) / "pooler"
    if new.is_dir() and any(new.iterdir()):
        return new
    if legacy.is_dir():
        return legacy
    return new


def resolve_kubernetes_metrics_cache_dir(run_dir: Path) -> Path:
    new = kubernetes_metrics_cache_dir(run_dir)
    legacy = kubernetes_dir(run_dir) / "cache"
    if new.is_dir() and any(new.iterdir()):
        return new
    if legacy.is_dir():
        return legacy
    return new


# --- Backward-compatible aliases (writers + readers) ------------------------


def kubernetes_cluster_dir(run_dir: Path) -> Path:
    """Write/read cluster metrics (``metrics/cluster/``)."""
    return kubernetes_metrics_cluster_dir(run_dir)


def kubernetes_pods_dir(run_dir: Path) -> Path:
    """Write/read pod logs (``logs/``)."""
    return kubernetes_logs_dir(run_dir)


def kubernetes_database_dir(run_dir: Path) -> Path:
    """Write/read database metrics (``metrics/database/``)."""
    return kubernetes_metrics_database_dir(run_dir)


def kubernetes_pooler_dir(run_dir: Path) -> Path:
    """Write/read pooler metrics (``metrics/pooler/``)."""
    return kubernetes_metrics_pooler_dir(run_dir)


def kubernetes_cache_dir(run_dir: Path) -> Path:
    """Write/read cache metrics (``metrics/cache/``)."""
    return kubernetes_metrics_cache_dir(run_dir)


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

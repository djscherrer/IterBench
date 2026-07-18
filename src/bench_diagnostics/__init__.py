"""
Per-run diagnostics for BaxBench bench runs.

Start collectors via :func:`session.diagnostics_session_for_k8s`. Collectors
write under ``diagnostics/kubernetes/``, plus shared ``diagnostics/hosts/``
for Locust SSH machines.

See :mod:`paths` for the full layout.
"""

from .base import DiagnosticsCollector, DiagnosticsSession
from .config import DiagnosticsMode, KubernetesDiagnosticsConfig
from .hosts import LoadHostMetricsCollector
from .kubernetes import (
    ClusterDiagnostics,
    PodLogStream,
    PodLogsCollector,
    PostgresMetricsCollector,
)
from .paths import (
    diagnostics_dir,
    kubernetes_logs_dir,
    kubernetes_metrics_dir,
    kubernetes_metrics_cluster_dir,
    kubernetes_metrics_database_dir,
    kubernetes_metrics_pooler_dir,
    kubernetes_metrics_cache_dir,
    resolve_kubernetes_logs_dir,
    resolve_kubernetes_metrics_cluster_dir,
    resolve_kubernetes_metrics_database_dir,
    resolve_kubernetes_metrics_pooler_dir,
    resolve_kubernetes_metrics_cache_dir,
    kubernetes_dir,
    kubernetes_pods_dir,
    kubernetes_cluster_dir,
    kubernetes_database_dir,
    kubernetes_pooler_dir,
    kubernetes_cache_dir,
    load_host_dir,
)
from .session import diagnostics_session, diagnostics_session_for_k8s
from .summary import (
    DiagnosticsSummary,
    benchmark_context_from_config,
    summarize_run_dir,
)

__all__ = [
    "DiagnosticsSummary",
    "benchmark_context_from_config",
    "summarize_run_dir",
    "ClusterDiagnostics",
    "DiagnosticsCollector",
    "DiagnosticsMode",
    "DiagnosticsSession",
    "KubernetesDiagnosticsConfig",
    "LoadHostMetricsCollector",
    "PodLogStream",
    "PodLogsCollector",
    "PostgresMetricsCollector",
    "diagnostics_dir",
    "diagnostics_session",
    "diagnostics_session_for_k8s",
    "kubernetes_logs_dir",
    "kubernetes_metrics_dir",
    "kubernetes_metrics_cluster_dir",
    "kubernetes_metrics_database_dir",
    "kubernetes_metrics_pooler_dir",
    "kubernetes_metrics_cache_dir",
    "resolve_kubernetes_logs_dir",
    "resolve_kubernetes_metrics_cluster_dir",
    "resolve_kubernetes_metrics_database_dir",
    "resolve_kubernetes_metrics_pooler_dir",
    "resolve_kubernetes_metrics_cache_dir",
    "kubernetes_cluster_dir",
    "kubernetes_cache_dir",
    "kubernetes_database_dir",
    "kubernetes_pooler_dir",
    "kubernetes_dir",
    "kubernetes_pods_dir",
    "load_host_dir",
]

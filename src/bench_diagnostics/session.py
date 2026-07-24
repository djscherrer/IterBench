"""
Build a :class:`DiagnosticsSession` for a bench run.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

from .base import DiagnosticsCollector, DiagnosticsSession
from .config import DiagnosticsMode, KubernetesDiagnosticsConfig
from .hosts import LoadHostMetricsCollector
from .kubernetes.cluster import ClusterDiagnostics
from .kubernetes.cache import RedisMetricsCollector
from .kubernetes.database import PostgresMetricsCollector
from .kubernetes.pooler import PgBouncerMetricsCollector
from .kubernetes.replication import ReplicationMetricsCollector
from .kubernetes.pods import PodLogStream, PodLogsCollector


def _resolve_k8s_pod_db_flag(default: bool) -> bool:
    raw = os.environ.get("BAXBENCH_K8S_DIAGNOSTICS", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def diagnostics_session(
    run_dir: Path,
    *,
    mode: DiagnosticsMode,
    load_hosts: Sequence[str],
    interval_s: int = 2,
    db_interval_s: int = 1,
    logger: logging.Logger | None = None,
    kubernetes: KubernetesDiagnosticsConfig | None = None,
) -> DiagnosticsSession:
    """
    Start diagnostics collectors for one bench run.

    Parameters
    ----------
    mode:
        ``DiagnosticsMode.KUBERNETES``.
    load_hosts:
        SSH hostnames for the Locust master + workers.
    interval_s:
        Sampling cadence for host/cluster metrics (``kubectl top`` is itself
        rate-limited by metrics-server, so finer than ~2s adds little).
    db_interval_s:
        Sampling cadence for Postgres ``pg_stat_*`` — kept tighter (1s) because
        connection-pool saturation is bursty and easily missed at coarser rates.
    kubernetes:
        Required when ``mode`` is ``KUBERNETES``.
    """
    collectors: list[DiagnosticsCollector] = [
        LoadHostMetricsCollector(
            run_dir,
            load_hosts,
            interval_s=interval_s,
            logger=logger,
        )
    ]

    if mode == DiagnosticsMode.KUBERNETES:
        if kubernetes is None:
            raise ValueError("kubernetes config is required for DiagnosticsMode.KUBERNETES")
        k8s = kubernetes
        pod_and_db = _resolve_k8s_pod_db_flag(k8s.pod_and_db_diagnostics)
        collectors.append(
            ClusterDiagnostics(
                run_dir,
                namespace=k8s.namespace,
                interval_s=interval_s,
                logger=logger,
            )
        )
        if pod_and_db:
            db_enabled = bool((k8s.db_service_name or "").strip())
            streams: list[PodLogStream] = [
                PodLogStream(name="backend", selector=k8s.backend_label_selector),
            ]
            if db_enabled:
                streams.append(
                    PodLogStream(
                        name="postgres",
                        selector=f"app={k8s.db_service_name}",
                        max_log_requests=4,
                    )
                )
                if k8s.db_replicas > 1:
                    streams.append(
                        PodLogStream(
                            name="postgres-replica",
                            selector="baxbench.dev/db-tier=replica",
                            max_log_requests=8,
                        )
                    )
            if k8s.pooler_enabled:
                streams.append(
                    PodLogStream(
                        name="pgbouncer",
                        selector="baxbench.dev/role=pooler",
                        max_log_requests=8,
                    )
                )
            if k8s.read_pooler_enabled:
                streams.append(
                    PodLogStream(
                        name="pgbouncer-read",
                        selector="baxbench.dev/role=read-pooler",
                        max_log_requests=8,
                    )
                )
            if k8s.cache_enabled:
                streams.append(
                    PodLogStream(
                        name="redis",
                        selector="baxbench.dev/role=cache",
                        max_log_requests=4,
                    )
                )
            collectors.append(
                PodLogsCollector(
                    run_dir,
                    namespace=k8s.namespace,
                    streams=streams,
                    logger=logger,
                )
            )
            if db_enabled:
                collectors.append(
                    PostgresMetricsCollector(
                        run_dir,
                        namespace=k8s.namespace,
                        label_selector=f"app={k8s.db_service_name}",
                        user=k8s.db_user,
                        password=k8s.db_password,
                        database=k8s.db_name,
                        interval_s=db_interval_s,
                        logger=logger,
                    )
                )
                if k8s.db_replicas > 1:
                    collectors.append(
                        ReplicationMetricsCollector(
                            run_dir,
                            namespace=k8s.namespace,
                            label_selector=f"app={k8s.db_service_name}",
                            user=k8s.db_user,
                            password=k8s.db_password,
                            database=k8s.db_name,
                            interval_s=db_interval_s,
                            logger=logger,
                        )
                    )
                if k8s.pooler_enabled or k8s.read_pooler_enabled:
                    collectors.append(
                        PgBouncerMetricsCollector(
                            run_dir,
                            namespace=k8s.namespace,
                            user=k8s.db_user,
                            password=k8s.db_password,
                            pooler_port=k8s.pooler_port,
                            read_pooler_port=k8s.read_pooler_port,
                            pooler_enabled=k8s.pooler_enabled,
                            read_pooler_enabled=k8s.read_pooler_enabled,
                            interval_s=db_interval_s,
                            logger=logger,
                        )
                    )
            if k8s.cache_enabled:
                collectors.append(
                    RedisMetricsCollector(
                        run_dir,
                        namespace=k8s.namespace,
                        interval_s=db_interval_s,
                        logger=logger,
                    )
                )

    else:
        raise ValueError(f"unsupported diagnostics mode: {mode!r}")

    return DiagnosticsSession(collectors)


def diagnostics_session_for_k8s(
    run_dir: Path,
    *,
    load_hosts: Sequence[str],
    namespace: str,
    interval_s: int = 2,
    db_interval_s: int = 1,
    logger: logging.Logger | None = None,
    pod_and_db_diagnostics: bool = True,
    db_service_name: str | None = None,
    db_user: str = "postgres",
    db_password: str = "postgres",
    db_name: str = "testdb",
    backend_label_selector: str = "app=backend",
    db_replicas: int = 1,
    pooler_enabled: bool = False,
    read_pooler_enabled: bool = False,
    cache_enabled: bool = False,
    pooler_port: int = 6432,
    read_pooler_port: int = 6432,
) -> DiagnosticsSession:
    """Convenience wrapper for k8s-bench."""
    return diagnostics_session(
        run_dir,
        mode=DiagnosticsMode.KUBERNETES,
        load_hosts=load_hosts,
        interval_s=interval_s,
        db_interval_s=db_interval_s,
        logger=logger,
        kubernetes=KubernetesDiagnosticsConfig(
            namespace=namespace,
            db_service_name=db_service_name,
            db_user=db_user,
            db_password=db_password,
            db_name=db_name,
            backend_label_selector=backend_label_selector,
            pod_and_db_diagnostics=pod_and_db_diagnostics,
            db_replicas=db_replicas,
            pooler_enabled=pooler_enabled,
            read_pooler_enabled=read_pooler_enabled,
            cache_enabled=cache_enabled,
            pooler_port=pooler_port,
            read_pooler_port=read_pooler_port,
        ),
    )

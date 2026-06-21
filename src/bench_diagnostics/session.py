"""
Build a :class:`DiagnosticsSession` for a bench run.

Pass :class:`DiagnosticsMode` to select which backend subtree is written.
Only collectors (and directories) for the active mode are started — a
kubernetes run never creates ``diagnostics/distributed/``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

from .base import DiagnosticsCollector, DiagnosticsSession
from .config import (
    DiagnosticsMode,
    DistributedDiagnosticsConfig,
    KubernetesDiagnosticsConfig,
)
from .distributed.hosts import WorkloadHostMetricsCollector, WorkloadHostSpec
from .hosts import LoadHostMetricsCollector
from .kubernetes.cluster import ClusterDiagnostics
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


def _workload_specs(cfg: DistributedDiagnosticsConfig) -> list[WorkloadHostSpec]:
    backend_set = {h for h in cfg.backend_hosts if h}
    db_host = (cfg.db_host or "").strip() or None
    lb_host = (cfg.lb_host or "").strip() or None
    containers = dict(cfg.backend_container_names)

    ordered: list[str] = []
    seen: set[str] = set()
    for h in list(cfg.backend_hosts) + ([db_host] if cfg.needs_db and db_host else []) + (
        [lb_host] if lb_host else []
    ):
        if h and h not in seen:
            seen.add(h)
            ordered.append(h)

    specs: list[WorkloadHostSpec] = []
    for h in ordered:
        if h in backend_set:
            container = containers.get(h)
        elif cfg.needs_db and h == db_host:
            container = cfg.db_container_name
        elif h == lb_host:
            container = cfg.lb_container_name
        else:
            container = None

        ports: list[int] = []
        if h in backend_set or h == lb_host:
            ports.append(int(cfg.app_port))
        if cfg.needs_db and h == db_host:
            ports.append(5432)

        specs.append(
            WorkloadHostSpec(host=h, docker_container=container, socket_ports=tuple(ports))
        )
    return specs


def diagnostics_session(
    run_dir: Path,
    *,
    mode: DiagnosticsMode,
    load_hosts: Sequence[str],
    interval_s: int = 2,
    db_interval_s: int = 1,
    logger: logging.Logger | None = None,
    kubernetes: KubernetesDiagnosticsConfig | None = None,
    distributed: DistributedDiagnosticsConfig | None = None,
) -> DiagnosticsSession:
    """
    Start diagnostics collectors for one bench run.

    Parameters
    ----------
    mode:
        ``DiagnosticsMode.KUBERNETES`` or ``DiagnosticsMode.DISTRIBUTED``.
    load_hosts:
        SSH hostnames for the Locust master + workers (both modes).
    interval_s:
        Sampling cadence for host/cluster metrics (``kubectl top`` is itself
        rate-limited by metrics-server, so finer than ~2s adds little).
    db_interval_s:
        Sampling cadence for Postgres ``pg_stat_*`` — kept tighter (1s) because
        connection-pool saturation is bursty and easily missed at coarser rates.
    kubernetes:
        Required when ``mode`` is ``KUBERNETES``.
    distributed:
        Required when ``mode`` is ``DISTRIBUTED``.
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
                collectors.append(
                    PgBouncerMetricsCollector(
                        run_dir,
                        namespace=k8s.namespace,
                        user=k8s.db_user,
                        password=k8s.db_password,
                        pooler_port=k8s.pooler_port,
                        read_pooler_port=k8s.read_pooler_port,
                        interval_s=db_interval_s,
                        logger=logger,
                    )
                )

    elif mode == DiagnosticsMode.DISTRIBUTED:
        if distributed is None:
            raise ValueError("distributed config is required for DiagnosticsMode.DISTRIBUTED")
        specs = _workload_specs(distributed)
        if specs:
            collectors.append(
                WorkloadHostMetricsCollector(
                    run_dir,
                    specs,
                    interval_s=interval_s,
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
            pooler_port=pooler_port,
            read_pooler_port=read_pooler_port,
        ),
    )


def diagnostics_session_for_distributed(
    run_dir: Path,
    *,
    load_hosts: Sequence[str],
    backend_hosts: Sequence[str],
    app_port: int,
    needs_db: bool,
    db_host: str | None = None,
    lb_host: str | None = None,
    backend_container_names: dict[str, str] | None = None,
    db_container_name: str | None = None,
    lb_container_name: str | None = None,
    interval_s: int = 2,
    logger: logging.Logger | None = None,
) -> DiagnosticsSession:
    """Convenience wrapper for distributed_bench."""
    return diagnostics_session(
        run_dir,
        mode=DiagnosticsMode.DISTRIBUTED,
        load_hosts=load_hosts,
        interval_s=interval_s,
        logger=logger,
        distributed=DistributedDiagnosticsConfig(
            backend_hosts=backend_hosts,
            app_port=app_port,
            needs_db=needs_db,
            db_host=db_host,
            lb_host=lb_host,
            backend_container_names=backend_container_names or {},
            db_container_name=db_container_name,
            lb_container_name=lb_container_name,
        ),
    )

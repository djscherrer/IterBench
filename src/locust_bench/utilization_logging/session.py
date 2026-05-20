"""
Build utilization sessions for a perf run.

Three loggers, two roles:

- **Load hosts** (``LoadHostUtilizationLogger``) — always on when Locust runs (k8s or distributed).
- **Workload** — exactly one of:
  - ``KubernetesUtilizationLogger`` (k8s-bench), or
  - ``DistributedBenchUtilizationLogger`` (distributed_bench).

K8s and distributed workload loggers are mutually exclusive.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Sequence

from ..load_topology import LoadTopology
from .distributed import DistributedBenchUtilizationLogger
from .kubernetes import KubernetesUtilizationLogger
from .load_hosts import LoadHostUtilizationLogger
from .base import UtilizationSession


def _load_logger(
    run_dir: Path,
    topology: LoadTopology,
    *,
    logger: logging.Logger | None,
    interval_s: int,
) -> LoadHostUtilizationLogger:
    return LoadHostUtilizationLogger(
        run_dir,
        topology.all_hosts,
        interval_s=interval_s,
        logger=logger,
    )


def utilization_session_for_k8s(
    run_dir: Path,
    *,
    load_topology: LoadTopology,
    namespace: str,
    interval_s: int = 5,
    logger: logging.Logger | None = None,
) -> UtilizationSession:
    """Locust load-host SSH stats + Kubernetes pod/node ``kubectl top``."""
    return UtilizationSession(
        [
            _load_logger(run_dir, load_topology, logger=logger, interval_s=interval_s),
            KubernetesUtilizationLogger(
                run_dir,
                namespace=namespace,
                interval_s=interval_s,
                logger=logger,
            ),
        ]
    )


def utilization_session_for_distributed(
    run_dir: Path,
    *,
    load_topology: LoadTopology,
    backend_hosts: Sequence[str],
    app_port: int,
    needs_db: bool,
    db_host: str | None = None,
    lb_host: str | None = None,
    backend_container_names: Mapping[str, str] | None = None,
    db_container_name: str | None = None,
    lb_container_name: str | None = None,
    interval_s: int = 5,
    logger: logging.Logger | None = None,
) -> UtilizationSession:
    """Locust load-host SSH stats + distributed app/DB/LB SSH/Docker stats."""
    return UtilizationSession(
        [
            _load_logger(run_dir, load_topology, logger=logger, interval_s=interval_s),
            DistributedBenchUtilizationLogger(
                run_dir,
                backend_hosts=backend_hosts,
                app_port=app_port,
                needs_db=needs_db,
                db_host=db_host,
                lb_host=lb_host,
                backend_container_names=backend_container_names,
                db_container_name=db_container_name,
                lb_container_name=lb_container_name,
                interval_s=interval_s,
                logger=logger,
            ),
        ]
    )

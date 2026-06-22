"""Configuration types for :func:`bench_diagnostics.session.diagnostics_session`."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence


class DiagnosticsMode(str, Enum):
    """Which backend subtree under ``diagnostics/`` is active for a run."""

    KUBERNETES = "kubernetes"
    DISTRIBUTED = "distributed"


@dataclass(frozen=True)
class KubernetesDiagnosticsConfig:
    """Settings for cluster / pod / database collectors (k8s-bench)."""

    namespace: str
    db_service_name: str | None = None
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "testdb"
    backend_label_selector: str = "app=backend"
    pod_and_db_diagnostics: bool = True
    db_replicas: int = 1
    pooler_enabled: bool = False
    read_pooler_enabled: bool = False
    cache_enabled: bool = False
    pooler_port: int = 6432
    read_pooler_port: int = 6432


@dataclass(frozen=True)
class DistributedDiagnosticsConfig:
    """Settings for workload-host collectors (distributed_bench)."""

    backend_hosts: Sequence[str]
    app_port: int
    needs_db: bool
    db_host: str | None = None
    lb_host: str | None = None
    backend_container_names: Mapping[str, str] = field(default_factory=dict)
    db_container_name: str | None = None
    lb_container_name: str | None = None

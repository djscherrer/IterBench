"""
Load-test execution for BaxBench (Locust runner, profiles, shapes).

This package is separate from :mod:`k8s_bench` (cluster deploy/orchestration).
It owns how load is generated and measured:

- ``locust_run.py`` — remote master/worker Locust + ``LocustRunner``
- ``paths.py`` — Locust artifact paths under ``locust/``
- ``load_profiles/`` — load shapes and ``baxbench_load_profile.json`` manifests

Bench observability lives in the top-level :mod:`bench_diagnostics` package.
"""

from .load_profiles import (
    LOAD_PROFILE_REGISTRY,
    AdaptiveLoadProfile,
    BaseLoadProfile,
    ContinuousLoadProfile,
    LoadProfile,
    SpikeLoadProfile,
    StairsLoadProfile,
    SteadyLoadProfile,
    resolve_load_profile,
)
from .load_topology import LoadTopology
from .locust_run import (
    DistributedLocustConfig,
    DistributedLocustSession,
    LocustRunner,
    prepare_locust_run_dir,
    resolve_locust_user_class,
)
from .paths import locust_csv_prefix, locust_dir, locust_logs_dir, locust_results_dir

__all__ = [
    "LOAD_PROFILE_REGISTRY",
    "AdaptiveLoadProfile",
    "BaseLoadProfile",
    "ContinuousLoadProfile",
    "DistributedLocustConfig",
    "DistributedLocustSession",
    "LoadProfile",
    "LoadTopology",
    "LocustRunner",
    "SpikeLoadProfile",
    "StairsLoadProfile",
    "SteadyLoadProfile",
    "locust_csv_prefix",
    "locust_dir",
    "locust_logs_dir",
    "locust_results_dir",
    "prepare_locust_run_dir",
    "resolve_load_profile",
    "resolve_locust_user_class",
]

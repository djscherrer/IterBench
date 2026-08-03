"""Bulk re-benchmarking / reverification of existing k8s-bench iteration results.

Pure discovery/manifest/cleanup logic lives here and is safe to unit test
without a cluster. The orchestration entry point is
``scripts/k8s_rebench_results.py``, which wires this package to the existing
deploy-only stages (``k8s_bench.orchestration.deploy_only``) — it reuses
those unmodified rather than duplicating deployment or benchmark logic.
"""

from .cleanup import (
    UnsafeDeletionError,
    clear_stale_reverify_artifacts,
    safe_rmtree,
    safe_unlink,
    stale_artifact_targets,
)
from .discovery import (
    DiscoveredIteration,
    DiscoveryFilters,
    DiscoveryReport,
    SkippedIteration,
    discover_iterations,
    group_by_sample_experiment,
)
from .manifest import (
    ManifestEntry,
    ManifestStore,
    already_reverified,
    load_manifest,
    manifest_key,
    path_key,
    utc_now,
    write_manifest,
)
from .metadata import TaskMetadata, parse_task_metadata

__all__ = [
    "UnsafeDeletionError",
    "clear_stale_reverify_artifacts",
    "safe_rmtree",
    "safe_unlink",
    "stale_artifact_targets",
    "DiscoveredIteration",
    "DiscoveryFilters",
    "DiscoveryReport",
    "SkippedIteration",
    "discover_iterations",
    "group_by_sample_experiment",
    "ManifestEntry",
    "ManifestStore",
    "already_reverified",
    "load_manifest",
    "manifest_key",
    "path_key",
    "utc_now",
    "write_manifest",
    "TaskMetadata",
    "parse_task_metadata",
]

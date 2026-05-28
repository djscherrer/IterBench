"""
Kubernetes-backed benchmark path (parallel to ``distributed_bench``).

Top level: orchestration (handler, loop, iteration), ``workspace``, and CLI.
Supporting packages: ``spec/`` (workload YAML), ``cluster/`` (kubectl, registry, lab).
"""

from .cluster import (
    ClusterCapacity,
    collect_cluster_capacity,
    deploy_iteration,
    ensure_k8s_cluster_ready,
)
from .iteration import (
    make_k8s_perf_run_dir,
    resolve_iterations_to_run,
    run_k8s_bench_iteration,
)
from .workspace import (
    ITERATIONS_DIRNAME,
    K8S_EXPERIMENTS_DIRNAME,
    deploy_bench_record_path,
    deploy_probe_record_path,
    default_k8s_namespace,
    find_iteration_spec_path,
    iteration_dir,
    iteration_manifests_dir,
    iteration_spec_path,
    iterations_root,
    k8s_workspace_root,
    new_iteration_id,
    normalize_experiment_id,
    resolve_bench_dir,
    resolve_iteration_dir,
    resolve_k8s_experiment_id,
)
from .spec import (
    K8sWorkloadSpec,
    generate_k8s_workload_spec,
    prepare_iteration,
    render_iteration,
    render_manifests,
)

__all__ = [
    "ClusterCapacity",
    "ITERATIONS_DIRNAME",
    "K8S_EXPERIMENTS_DIRNAME",
    "K8sWorkloadSpec",
    "collect_cluster_capacity",
    "deploy_bench_record_path",
    "deploy_iteration",
    "deploy_probe_record_path",
    "ensure_k8s_cluster_ready",
    "find_iteration_spec_path",
    "generate_k8s_workload_spec",
    "iteration_dir",
    "iteration_manifests_dir",
    "iteration_spec_path",
    "iterations_root",
    "default_k8s_namespace",
    "k8s_workspace_root",
    "normalize_experiment_id",
    "resolve_bench_dir",
    "resolve_iteration_dir",
    "resolve_k8s_experiment_id",
    "make_k8s_perf_run_dir",
    "new_iteration_id",
    "prepare_iteration",
    "render_iteration",
    "render_manifests",
    "resolve_iterations_to_run",
    "run_k8s_bench_iteration",
]

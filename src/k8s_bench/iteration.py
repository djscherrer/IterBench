"""Single K8s deploy + Locust run for one ``k8s_configs/<iteration>/`` directory."""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from locust_bench.locust_run import (
    DistributedLocustConfig,
    DistributedLocustSession,
    prepare_locust_run_dir,
)
from locust_bench.load_profiles import resolve_load_profile
from locust_bench.load_topology import LoadTopology
from locust_bench.utilization_logging import utilization_session_for_k8s

from .cluster.deploy import DeployResult, deploy_iteration
from .cluster.images import prepare_image_for_k8s
from .cluster.load_target import resolve_nodeport_target
from .cluster.profiles import selected_cluster_profile
from .spec.models import BackendSpec, DatabaseSpec, K8sWorkloadSpec
from .paths import (
    default_k8s_namespace,
    iteration_spec_path,
    k8s_configs_root,
    list_iteration_dirs,
    new_iteration_id,
    normalize_iteration_id,
    perf_run_dir_for_iteration,
    resolve_k8s_experiment_id,
)
from .spec.render import render_iteration


def _slugify_run_part(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", s.strip()) or "default"


def resolve_iterations_to_run(
    sample_dir: Path,
    *,
    iteration_id: str | None,
    auto_init: bool,
) -> list[Path]:
    if iteration_id:
        path = k8s_configs_root(sample_dir) / normalize_iteration_id(iteration_id)
        if not (path / "spec.yaml").is_file():
            raise FileNotFoundError(f"Missing spec.yaml for iteration: {path}")
        return [path]
    existing = list_iteration_dirs(sample_dir)
    if existing:
        return existing
    if not auto_init:
        raise FileNotFoundError(
            f"No k8s iterations under {k8s_configs_root(sample_dir)}; "
            "pass --k8s-iteration or enable auto-init."
        )
    iid = new_iteration_id(sample_dir)
    path = k8s_configs_root(sample_dir) / iid
    path.mkdir(parents=True, exist_ok=True)
    return [path]


def ensure_iteration_spec(
    iteration_path: Path,
    *,
    image_reference: str,
    app_port: int,
    needs_db: bool,
    labels: dict[str, str] | None = None,
) -> K8sWorkloadSpec:
    spec_path = iteration_spec_path(iteration_path)
    iid = iteration_path.name
    if spec_path.is_file():
        spec = K8sWorkloadSpec.from_yaml_file(spec_path)
        updated = K8sWorkloadSpec(
            iteration_id=spec.iteration_id,
            namespace=spec.namespace,
            backend=BackendSpec(
                image=image_reference,
                replicas=spec.backend.replicas,
                port=spec.backend.port or app_port,
                resources=spec.backend.resources,
                env=spec.backend.env,
            ),
            database=DatabaseSpec(
                enabled=needs_db if needs_db else spec.database.enabled,
                image=spec.database.image,
                service_name=spec.database.service_name,
                port=spec.database.port,
                resources=spec.database.resources,
            ),
            labels={**spec.labels, **(labels or {})},
        )
    else:
        updated = K8sWorkloadSpec(
            iteration_id=iid,
            namespace=default_k8s_namespace(iid),
            backend=BackendSpec(image=image_reference, port=app_port),
            database=DatabaseSpec(enabled=needs_db),
            labels=labels or {},
        )
    updated.write_yaml(spec_path)
    return updated


def write_k8s_run_config(
    run_dir: Path,
    *,
    spec: K8sWorkloadSpec,
    deploy_result: DeployResult,
    load_profile: str,
    iteration_path: Path,
    image_reference: str,
    locust_target: str,
    load_topology: LoadTopology,
) -> None:
    experiment_id = resolve_k8s_experiment_id()
    snapshot: dict[str, Any] = {
        "deploy_target": "kubernetes",
        "requested_profiles": {"load_profile": load_profile},
        "k8s_experiment": experiment_id,
        "k8s_iteration": {
            "id": spec.iteration_id,
            "path": str(iteration_path),
            "namespace": spec.namespace,
        },
        "k8s_workload_spec": spec.to_yaml_dict(),
        "deploy_result": deploy_result.to_dict(),
        "image_reference": image_reference,
        "locust_target": locust_target,
        "load_topology": {
            "master": load_topology.master,
            "workers": list(load_topology.workers),
        },
    }
    (run_dir / "config.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _remote_load_paths(sample_slug: str) -> tuple[str, str]:
    base = f"/tmp/baxbench-k8s-load/{_slugify_run_part(sample_slug)}"
    return base, f"{base}/.venv"


def run_k8s_bench_iteration(
    *,
    iteration_path: Path,
    run_dir: Path,
    image_id: str,
    sample_slug: str,
    app_port: int,
    needs_db: bool,
    locustfile: Path,
    csv_prefix: Path,
    timeout: int,
    locust_user: str,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    wait_timeout_s: int,
    labels: dict[str, str] | None,
    logger: logging.Logger,
) -> DeployResult:
    del timeout, locust_user

    profile = selected_cluster_profile()
    if not profile.has_load_topology():
        raise ValueError(
            f"K8s profile '{profile.name}' has no load_master; "
            "set load_master/load_workers in profiles.py"
        )

    prepared = prepare_image_for_k8s(
        image_id,
        sample_slug=sample_slug,
        profile_name=profile.name,
        logger=logger,
    )
    spec = ensure_iteration_spec(
        iteration_path,
        image_reference=prepared.reference,
        app_port=app_port,
        needs_db=needs_db,
        labels=labels,
    )
    render_iteration(iteration_path)
    deploy_result = deploy_iteration(iteration_path, wait_timeout_s=wait_timeout_s, logger=logger)
    if not deploy_result.success:
        raise RuntimeError(f"Kubernetes deploy failed for {iteration_path}")

    entry_node = profile.worker_nodes[0] if profile.worker_nodes else profile.control_node
    target_base_url = resolve_nodeport_target(
        namespace=spec.namespace,
        service="backend",
        service_port=spec.backend.port,
        node_host=entry_node,
        logger=logger,
    )

    topology = LoadTopology.from_profile_fields(
        load_master=profile.load_master,
        load_workers=profile.load_workers,
    )
    load_profile = resolve_load_profile(os.environ.get("BAXBENCH_LOAD_PROFILE", "default"))
    run_time_s = (
        int(bench_run_time) if bench_run_time is not None else int(load_profile.effective_run_time_s)
    )
    users = int(bench_users) if bench_users is not None else int(load_profile.effective_users)
    spawn_rate = (
        int(bench_spawn_rate) if bench_spawn_rate is not None else int(load_profile.effective_spawn_rate)
    )

    local_locust = prepare_locust_run_dir(run_dir, locustfile)
    remote_load_dir, remote_env_dir = _remote_load_paths(sample_slug)

    write_k8s_run_config(
        run_dir,
        spec=spec,
        deploy_result=deploy_result,
        load_profile=os.environ.get("BAXBENCH_LOAD_PROFILE", "default"),
        iteration_path=iteration_path,
        image_reference=prepared.reference,
        locust_target=target_base_url,
        load_topology=topology,
    )

    config = DistributedLocustConfig(
        topology=topology,
        locustfile=local_locust,
        csv_prefix=csv_prefix,
        remote_load_dir=remote_load_dir,
        remote_env_dir=remote_env_dir,
        app_port=spec.backend.port,
        bench_users=users,
        bench_spawn_rate=spawn_rate,
        bench_run_time_s=run_time_s,
        locust_run_time=f"{run_time_s}s",
        load_profile=load_profile,
        target_base_url=target_base_url,
        sample_dir=run_dir,
        sample_slug=_slugify_run_part(sample_slug),
        logger=logger,
    )
    utilization = utilization_session_for_k8s(
        run_dir,
        load_topology=topology,
        namespace=spec.namespace,
        logger=logger,
    )

    logger.info(
        "Running distributed Locust: master=%s workers=%s target=%s",
        topology.master,
        ",".join(topology.workers) or "(none)",
        target_base_url,
    )
    with utilization:
        DistributedLocustSession(config).run(target=entry_node)

    return deploy_result


def make_k8s_perf_run_dir(
    sample_dir: Path,
    iteration_id: str,
    *,
    load_profile: str | None = None,
) -> Path:
    prof = _slugify_run_part(load_profile or os.environ.get("BAXBENCH_LOAD_PROFILE", "default"))
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return perf_run_dir_for_iteration(sample_dir, iteration_id, load_profile=prof, timestamp=ts)

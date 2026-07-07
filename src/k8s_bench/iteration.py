"""Locust bench for one ``iterations/NNN/`` directory (deploy is a separate stage)."""

from __future__ import annotations

import datetime
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from locust_bench.locust_run import (
    DistributedLocustConfig,
    DistributedLocustSession,
    prepare_locust_run_dir,
)
from bench_diagnostics import diagnostics_session_for_k8s
from locust_bench.load_profiles import resolve_load_profile
from locust_bench.load_profiles.manifest import build_load_profile_manifest
from locust_bench.load_topology import LoadTopology

from .cluster.deploy import DeployResult
from .cluster.load_target import resolve_nodeport_target
from .cluster.profiles import selected_cluster_profile
from .spec.models import (
    BackendSpec,
    DatabaseSpec,
    K8sWorkloadSpec,
    POSTGRES_DATABASE,
    POSTGRES_PASSWORD,
    POSTGRES_USER,
)
from .workspace import (
    default_k8s_namespace,
    deploy_bench_record_path,
    deploy_probe_record_path,
    ensure_iteration_core_layout,
    experiment_root_from_iteration_path,
    find_iteration_spec_path,
    iteration_id_for_index,
    iteration_spec_path,
    iterations_root,
    list_iteration_dirs,
    new_iteration_id,
    normalize_iteration_id,
    parse_iteration_folder_name,
    perf_run_dir_for_iteration,
    resolve_iteration_dir,
)


def _slugify_run_part(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", s.strip()) or "default"


def load_probe_deploy_result(
    iteration_path: Path,
    *,
    logger: logging.Logger,
) -> DeployResult:
    """Load a successful deploy probe written by ``04-deploy``."""
    probe_path = deploy_probe_record_path(iteration_path)
    if not probe_path.is_file():
        raise RuntimeError(
            f"Missing deploy probe at {probe_path}; run the deploy stage before bench"
        )
    try:
        record = json.loads(probe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unreadable deploy probe: {probe_path}") from exc
    if not record.get("success"):
        raise RuntimeError(
            f"Deploy probe did not succeed for {iteration_path.name}; "
            "bench requires a successful deploy stage"
        )
    logger.info("Using deploy probe from %s", probe_path)
    return DeployResult(
        success=True,
        namespace=str(record.get("namespace") or ""),
        manifest_file=str(record.get("manifest_file") or ""),
        kubectl_context=record.get("kubectl_context"),
        backend_service_url=str(record.get("backend_service_url") or ""),
        applied_at=str(record.get("applied_at") or ""),
        stdout=str(record.get("stdout") or ""),
        stderr=str(record.get("stderr") or ""),
        wait_details=dict(record.get("wait_details") or {}),
    )


def _copy_probe_record_to_bench(iteration_path: Path) -> None:
    """Mirror ``04-deploy/probe.json`` to ``04-deploy/bench.json`` for downstream readers."""
    probe = deploy_probe_record_path(iteration_path)
    if not probe.is_file():
        return
    bench = deploy_bench_record_path(iteration_path)
    bench.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(probe, bench)


def resolve_iterations_to_run(
    sample_dir: Path,
    *,
    iteration_id: str | None,
    auto_init: bool,
    iteration_path: Path | None = None,
    experiment_id: str | None = None,
) -> list[Path]:
    if iteration_path is not None:
        path = Path(iteration_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Iteration path is not a directory: {path}")
        if find_iteration_spec_path(path) is None:
            raise FileNotFoundError(f"Missing spec for iteration: {path}")
        return [path]
    if iteration_id:
        path = resolve_iteration_dir(
            sample_dir, iteration_id, experiment_id=experiment_id
        )
        if find_iteration_spec_path(path) is None:
            raise FileNotFoundError(f"Missing spec for iteration: {path}")
        return [path]
    existing = list_iteration_dirs(sample_dir, experiment_id=experiment_id)
    if existing:
        return existing
    if not auto_init:
        raise FileNotFoundError(
            f"No k8s iterations under {iterations_root(sample_dir, experiment_id=experiment_id)}; "
            "pass --k8s-iteration or enable auto-init."
        )
    iid = new_iteration_id(sample_dir, experiment_id=experiment_id)
    path = resolve_iteration_dir(
        sample_dir, iid, experiment_id=experiment_id
    )
    ensure_iteration_core_layout(path)
    return [path]


def ensure_iteration_spec(
    iteration_path: Path,
    *,
    image_reference: str,
    app_port: int,
    needs_db: bool,
    labels: dict[str, str] | None = None,
) -> K8sWorkloadSpec:
    ensure_iteration_core_layout(iteration_path)
    spec_path = find_iteration_spec_path(iteration_path)
    idx, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
    iid = (
        iteration_id_for_index(idx)
        if idx is not None
        else normalize_iteration_id(iteration_path.name)
    )
    if spec_path is not None and spec_path.is_file():
        spec = K8sWorkloadSpec.from_yaml_file(spec_path)
        db = spec.database
        updated = K8sWorkloadSpec(
            iteration_id=spec.iteration_id,
            namespace=spec.namespace,
            backend=BackendSpec(
                image=image_reference,
                replicas=spec.backend.replicas,
                port=spec.backend.port or app_port,
                resources=spec.backend.resources,
                env=spec.backend.env,
                placement_workers=spec.backend.placement_workers,
                spread_replicas=spec.backend.spread_replicas,
            ),
            database=DatabaseSpec(
                enabled=needs_db if needs_db else db.enabled,
                image=db.image,
                service_name=db.service_name,
                port=db.port,
                replicas=db.replicas,
                max_connections=db.max_connections,
                tuning=db.tuning,
                placement_worker=db.placement_worker,
                placement_workers=db.placement_workers,
                resources=db.resources,
                primary_resources=db.primary_resources,
                replica_resources=db.replica_resources,
                cache=db.cache,
            ),
            pooler=spec.pooler,
            read_pooler=spec.read_pooler,
            cache=spec.cache,
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
    updated.write_yaml(iteration_spec_path(iteration_path))
    return updated


def write_k8s_run_config(
    run_dir: Path,
    *,
    spec: K8sWorkloadSpec,
    deploy_result: DeployResult,
    load_profile: str,
    resolved_load_profile: dict[str, Any],
    iteration_path: Path,
    image_reference: str,
    locust_target: str,
    load_topology: LoadTopology,
) -> None:
    experiment_id = experiment_root_from_iteration_path(iteration_path).name
    snapshot: dict[str, Any] = {
        "deploy_target": "kubernetes",
        "requested_profiles": {"load_profile": load_profile},
        "resolved_load_profile": resolved_load_profile,
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
    sample_slug: str,
    locustfile: Path,
    csv_prefix: Path,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    load_profile: str,
    k8s_cluster: str,
    logger: logging.Logger,
) -> DeployResult:
    """
    Run Locust + diagnostics for one iteration.

    Does **not** render manifests, ``kubectl apply``, or push images — callers
    must run ``04-deploy`` first (``load_probe_deploy_result``).
    """

    profile = selected_cluster_profile(k8s_cluster)
    if not profile.has_load_topology():
        raise ValueError(
            f"K8s profile '{profile.name}' has no load_master; "
            "set load_master/load_workers in profiles.py"
        )

    spec_yaml = find_iteration_spec_path(iteration_path)
    if spec_yaml is None:
        raise RuntimeError(f"Missing spec.yaml under {iteration_path}")
    spec = K8sWorkloadSpec.from_yaml_file(spec_yaml)

    deploy_result = load_probe_deploy_result(iteration_path, logger=logger)
    _copy_probe_record_to_bench(iteration_path)

    from .stages.deploy import check_service_endpoints_ready

    backend_ok, backend_msg = check_service_endpoints_ready(
        namespace=spec.namespace,
        service="backend",
        logger=logger,
    )
    if not backend_ok:
        raise RuntimeError(f"Cluster not ready for bench: {backend_msg}")

    image_reference = spec.backend.image

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
    load_profile_name = load_profile
    resolved_load_profile = resolve_load_profile(load_profile_name)
    run_time_s = (
        int(bench_run_time)
        if bench_run_time is not None
        else int(resolved_load_profile.effective_run_time_s)
    )
    users = (
        int(bench_users)
        if bench_users is not None
        else int(resolved_load_profile.effective_users)
    )
    spawn_rate = (
        int(bench_spawn_rate)
        if bench_spawn_rate is not None
        else int(resolved_load_profile.effective_spawn_rate)
    )

    local_locust = prepare_locust_run_dir(
        run_dir,
        locustfile,
        load_profile=resolved_load_profile,
        bench_run_time_s=run_time_s,
        bench_users=users if bench_users is not None else None,
    )
    remote_load_dir, remote_env_dir = _remote_load_paths(sample_slug)
    load_profile_manifest = build_load_profile_manifest(
        resolved_load_profile,
        bench_run_time_s=run_time_s,
        bench_users=users if bench_users is not None else None,
    )

    write_k8s_run_config(
        run_dir,
        spec=spec,
        deploy_result=deploy_result,
        load_profile=load_profile_name,
        resolved_load_profile=load_profile_manifest,
        iteration_path=iteration_path,
        image_reference=image_reference,
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
        load_profile=resolved_load_profile,
        target_base_url=target_base_url,
        sample_dir=run_dir,
        sample_slug=_slugify_run_part(sample_slug),
        logger=logger,
    )
    diagnostics = diagnostics_session_for_k8s(
        run_dir,
        load_hosts=topology.all_hosts,
        namespace=spec.namespace,
        logger=logger,
        db_service_name=spec.database.service_name if spec.database.enabled else None,
        db_user=POSTGRES_USER,
        db_password=POSTGRES_PASSWORD,
        db_name=POSTGRES_DATABASE,
        db_replicas=spec.database.replicas if spec.database.enabled else 1,
        pooler_enabled=spec.pooler.enabled if spec.database.enabled else False,
        read_pooler_enabled=(
            spec.read_pooler.enabled if spec.database.enabled else False
        ),
        cache_enabled=spec.cache.enabled,
        pooler_port=spec.pooler.port if spec.pooler.enabled else 6432,
        read_pooler_port=(
            spec.read_pooler.port if spec.read_pooler.enabled else 6432
        ),
    )

    logger.info(
        "Running distributed Locust: master=%s workers=%s target=%s",
        topology.master,
        ",".join(topology.workers) or "(none)",
        target_base_url,
    )
    with diagnostics:
        DistributedLocustSession(config).run(target=entry_node)

    return deploy_result


def make_k8s_perf_run_dir(
    sample_dir: Path,
    iteration_id: str,
    *,
    load_profile: str | None = None,
) -> Path:
    prof = _slugify_run_part(load_profile)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return perf_run_dir_for_iteration(
        sample_dir, iteration_id, load_profile=prof, timestamp=ts
    )

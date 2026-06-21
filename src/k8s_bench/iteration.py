"""Single K8s deploy + Locust run for one ``iterations/NNN/`` directory."""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from locust_bench.locust_run import (
    DistributedLocustConfig,
    DistributedLocustSession,
    prepare_locust_run_dir,
)
from bench_diagnostics import diagnostics_session_for_k8s
from locust_bench.load_profiles import resolve_load_profile
from locust_bench.load_topology import LoadTopology

from .cluster.deploy import DeployResult, deploy_iteration
from .cluster.images import (
    PreparedImage,
    expected_registry_reference,
    prepare_image_for_k8s,
)
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
    resolve_k8s_experiment_id,
)
from .spec.render import render_iteration


def _slugify_run_part(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", s.strip()) or "default"


def _namespace_exists(namespace: str, *, logger: logging.Logger) -> bool:
    """Cheap check that the iteration namespace still lives in the cluster."""
    proc = subprocess.run(
        ["kubectl", "get", "namespace", namespace, "-o", "name"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        logger.info(
            "namespace %s not found (rc=%s); cannot reuse probe deploy: %s",
            namespace,
            proc.returncode,
            (proc.stderr or proc.stdout).strip()[:200],
        )
        return False
    return True


def _probe_deploy_is_reusable(
    iteration_path: Path,
    spec: K8sWorkloadSpec,
    *,
    image_reference: str,
    logger: logging.Logger,
) -> DeployResult | None:
    """
    Return the probe :class:`DeployResult` when the bench can skip its own deploy.

    Reusable means:
    1. ``04-deploy/probe.json`` exists with ``success: true``.
    2. The recorded probe ran against the *same* manifest file we'd render now
       (``03-spec/manifests/all.yaml`` mtime older than probe ``applied_at``).
    3. The probe namespace still exists with ready ``backend`` endpoints (and
       ``postgres`` endpoints when the spec has the database enabled).
    4. The recorded probe used the same image reference as the one we just
       prepared (otherwise pods could still be running stale code).
    """
    probe_path = deploy_probe_record_path(iteration_path)
    if not probe_path.is_file():
        return None
    try:
        record = json.loads(probe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("probe.json unreadable, redeploying: %s", exc)
        return None
    if not record.get("success"):
        return None
    if record.get("namespace") != spec.namespace:
        logger.info(
            "probe.json namespace=%s != current spec namespace=%s; redeploying",
            record.get("namespace"),
            spec.namespace,
        )
        return None

    manifest_file = Path(record.get("manifest_file") or "")
    spec_yaml = iteration_spec_path(iteration_path)
    if (
        manifest_file.is_file()
        and spec_yaml.is_file()
        and manifest_file.stat().st_mtime < spec_yaml.stat().st_mtime - 1
    ):
        logger.info(
            "spec.yaml newer than probe manifest (%s); redeploying",
            manifest_file.name,
        )
        return None

    if not _namespace_exists(spec.namespace, logger=logger):
        return None

    # Late import to avoid the circular ``iteration → gates.deploy_probe →
    # iteration.ensure_iteration_spec`` dependency at module load time.
    from .gates.deploy_probe import check_service_endpoints_ready

    backend_ok, backend_msg = check_service_endpoints_ready(
        namespace=spec.namespace, service="backend", logger=logger
    )
    if not backend_ok:
        logger.info("probe deploy unhealthy (backend): %s", backend_msg)
        return None

    if spec.database.enabled:
        db_ok, db_msg = check_service_endpoints_ready(
            namespace=spec.namespace,
            service=spec.database.service_name,
            logger=logger,
        )
        if not db_ok:
            logger.info("probe deploy unhealthy (db): %s", db_msg)
            return None

    # Image-ref sanity check: pods may have already been scheduled against a
    # different image tag if the build was rebuilt since probe time.
    expected = image_reference.strip()
    actual_image = None
    backend_url = str(record.get("backend_service_url") or "")
    if backend_url and expected:
        # Cheap kubectl inspect, no rollout-restart.
        get = subprocess.run(
            [
                "kubectl",
                "get",
                "deployment",
                "backend",
                "-n",
                spec.namespace,
                "-o",
                "jsonpath={.spec.template.spec.containers[0].image}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        actual_image = (get.stdout or "").strip()
        if actual_image and actual_image != expected:
            logger.info(
                "deployed image %s differs from prepared %s; redeploying",
                actual_image,
                expected,
            )
            return None

    logger.info(
        "Reusing probe deploy for %s (namespace=%s, image=%s): %s",
        iteration_path.name,
        spec.namespace,
        actual_image or "<unchecked>",
        backend_msg,
    )
    return DeployResult(
        success=True,
        namespace=record.get("namespace") or spec.namespace,
        manifest_file=str(manifest_file),
        kubectl_context=record.get("kubectl_context"),
        backend_service_url=record.get("backend_service_url") or "",
        applied_at=record.get("applied_at") or "",
        stdout=record.get("stdout") or "",
        stderr=record.get("stderr") or "",
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
) -> list[Path]:
    if iteration_path is not None:
        path = Path(iteration_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Iteration path is not a directory: {path}")
        if find_iteration_spec_path(path) is None:
            raise FileNotFoundError(f"Missing spec for iteration: {path}")
        return [path]
    if iteration_id:
        path = resolve_iteration_dir(sample_dir, iteration_id)
        if find_iteration_spec_path(path) is None:
            raise FileNotFoundError(f"Missing spec for iteration: {path}")
        return [path]
    existing = list_iteration_dirs(sample_dir)
    if existing:
        return existing
    if not auto_init:
        raise FileNotFoundError(
            f"No k8s iterations under {iterations_root(sample_dir)}; "
            "pass --k8s-iteration or enable auto-init."
        )
    iid = new_iteration_id(sample_dir)
    path = resolve_iteration_dir(sample_dir, iid)
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
                web_concurrency=spec.backend.web_concurrency,
                worker_class=spec.backend.worker_class,
                worker_threads=spec.backend.worker_threads,
                preload=spec.backend.preload,
                max_requests=spec.backend.max_requests,
                max_requests_jitter=spec.backend.max_requests_jitter,
                backlog=spec.backend.backlog,
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

    # Fast path: when the probe just deployed this exact iteration we can
    # reuse the running namespace — no docker push, no kubectl apply, no
    # readiness wait. We test this *before* paying the push cost by computing
    # the registry reference deterministically.
    spec_yaml = find_iteration_spec_path(iteration_path)
    expected_ref = expected_registry_reference(
        image_id, sample_slug=sample_slug, profile_name=profile.name
    )
    reused: DeployResult | None = None
    prepared: PreparedImage | None = None

    if spec_yaml is not None and spec_yaml.is_file() and expected_ref is not None:
        candidate_spec = K8sWorkloadSpec.from_yaml_file(spec_yaml)
        reused = _probe_deploy_is_reusable(
            iteration_path,
            candidate_spec,
            image_reference=expected_ref,
            logger=logger,
        )
        if reused is not None:
            spec = candidate_spec
            prepared = PreparedImage(reference=expected_ref)
            deploy_result = reused
            _copy_probe_record_to_bench(iteration_path)

    if reused is None:
        # Probe did not run, isn't reusable, or no registry — full deploy path:
        # docker push → cleanup → render → kubectl apply → wait.
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
        deploy_result = deploy_iteration(
            iteration_path, wait_timeout_s=wait_timeout_s, logger=logger
        )
        if not deploy_result.success:
            raise RuntimeError(f"Kubernetes deploy failed for {iteration_path}")

    assert prepared is not None
    assert spec is not None

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
    prof = _slugify_run_part(load_profile or os.environ.get("BAXBENCH_LOAD_PROFILE", "default"))
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return perf_run_dir_for_iteration(
        sample_dir, iteration_id, load_profile=prof, timestamp=ts
    )

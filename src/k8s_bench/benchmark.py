from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .deploy import DeployResult, deploy_iteration
from .images import prepare_image_for_k8s
from .models import BackendSpec, DatabaseSpec, K8sWorkloadSpec
from .paths import (
    iteration_spec_path,
    k8s_configs_root,
    list_iteration_dirs,
    new_iteration_id,
    normalize_iteration_id,
    perf_run_dir_for_iteration,
)
from .portforward import kubectl_port_forward
from .render import render_iteration


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
            namespace=f"baxbench-{iid}",
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
    locust_host: str,
) -> None:
    snapshot: dict[str, Any] = {
        "deploy_target": "kubernetes",
        "requested_profiles": {"load_profile": load_profile},
        "k8s_iteration": {
            "id": spec.iteration_id,
            "path": str(iteration_path),
            "namespace": spec.namespace,
        },
        "k8s_workload_spec": spec.to_yaml_dict(),
        "deploy_result": deploy_result.to_dict(),
        "image_reference": image_reference,
        "locust_host": locust_host,
    }
    (run_dir / "config.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
    local_port: int | None,
    labels: dict[str, str] | None,
    logger: logging.Logger,
) -> DeployResult:
    profile_name = os.environ.get("BAXBENCH_K8S_CLUSTER", "").strip() or None
    prepared = prepare_image_for_k8s(
        image_id,
        sample_slug=sample_slug,
        profile_name=profile_name,
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

    locust_logs: bytes = b""
    with kubectl_port_forward(
        namespace=spec.namespace,
        service="backend",
        remote_port=spec.backend.port,
        local_port=local_port,
        logger=logger,
    ) as port:
        locust_host = f"http://127.0.0.1:{port}"
        write_k8s_run_config(
            run_dir,
            spec=spec,
            deploy_result=deploy_result,
            load_profile=os.environ.get("BAXBENCH_LOAD_PROFILE", "default"),
            iteration_path=iteration_path,
            image_reference=prepared.reference,
            locust_host=locust_host,
        )
        from locust_bench import run_headless_locust

        locust_logs = run_headless_locust(
            locustfile=locustfile,
            csv_prefix=csv_prefix,
            target_host=locust_host,
            timeout=timeout,
            locust_user=locust_user,
            bench_users=bench_users,
            bench_spawn_rate=bench_spawn_rate,
            bench_run_time=bench_run_time,
        )
    if locust_logs:
        logger.info("loader logs:\n%s", locust_logs.decode(errors="replace"))
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

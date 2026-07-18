"""
Locust bench stage (``05-bench/``).

Runs Locust load tests against an iteration that already passed ``04-deploy``.
On success, also writes iteration feedback, marks ``meta.json`` successful, and
appends the perf-run block to ``experiment_summary.md``.
Does not render manifests, ``kubectl apply``, build images, or push to the registry.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_diagnostics import diagnostics_session_for_k8s
from load_bench.load_profiles import resolve_load_profile
from load_bench.load_profiles.manifest import build_load_profile_manifest
from load_bench.load_topology import LoadTopology
from load_bench.locust_run import (
    DistributedLocustConfig,
    DistributedLocustSession,
    prepare_locust_run_dir,
)
from load_bench.paths import locust_csv_prefix

from ..cluster.deploy import DeployResult
from ..cluster.profiles import selected_cluster_profile
from ..experiment_summary import append_perf_run_block
from ..failure import BenchFailureRecord, fail_iteration_phase
from ..failure.bench_diagnostics import collect_bench_failure_diagnostics
from ..failure.classify import classify_bench_failure_kind
from ..failure.persist import build_bench_iteration_failure
from ..feedback import collect_iteration_feedback, read_failed_iteration_error_excerpt
from ..orchestration.config import IterationPlan, RunConfig, SampleContext
from ..spec.models import (
    K8sWorkloadSpec,
    POSTGRES_DATABASE,
    POSTGRES_PASSWORD,
    POSTGRES_USER,
)
from ..workspace import (
    deploy_probe_record_path,
    experiment_root_from_iteration_path,
    find_iteration_spec_path,
    iteration_bench_dir,
    iteration_bench_log_path,
    resolve_iteration_dir,
    update_iteration_meta,
    write_feedback,
)
from ..workspace.skips import append_k8s_skip


# ---------------------------------------------------------------------------
# Bench engine (Locust + diagnostics; deploy must have succeeded)
# ---------------------------------------------------------------------------

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
        image_reference=str(record.get("image_reference") or ""),
        backend_port=int(record.get("backend_port") or 0),
        nodeport_target=str(record.get("nodeport_target") or ""),
        deploy_labels=dict(record.get("deploy_labels") or {}),
    )


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


def run_distributed_locust(
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
    Run distributed Locust and diagnostics for one perf-test CSV prefix.

    Does **not** render manifests, ``kubectl apply``, or push images — callers
    must run ``04-deploy`` first.
    """
    profile = selected_cluster_profile(k8s_cluster)
    if not profile.has_load_topology():
        raise ValueError(
            f"K8s profile '{profile.name}' has no load_master; "
            "set load_master/load_workers in profiles.py"
        )

    # Runtime facts are the deploy probe's job (04-deploy). It is the source of truth
    # for namespace, image, backend port, and the external Locust target; bench never
    # re-derives them here.
    deploy_result = load_probe_deploy_result(iteration_path, logger=logger)
    namespace = deploy_result.namespace
    image_reference = deploy_result.image_reference
    backend_port = deploy_result.backend_port
    target_base_url = deploy_result.nodeport_target
    if not (namespace and backend_port and target_base_url):
        raise RuntimeError(
            f"Deploy probe for {iteration_path.name} is missing runtime fields "
            "(namespace/backend_port/nodeport_target); re-run the deploy stage"
        )

    # spec.yaml is the LLM workload plan; bench reads it only to configure diagnostics
    # collection (DB/pooler/cache topology), which is not stored in the probe.
    spec_yaml = find_iteration_spec_path(iteration_path)
    if spec_yaml is None:
        raise RuntimeError(f"Missing spec.yaml under {iteration_path}")
    spec = K8sWorkloadSpec.from_yaml_file(spec_yaml)

    # Sanity check the cluster is still serving before spinning up load generators
    # (pods may have died between deploy and bench).
    from .deploy import check_service_endpoints_ready

    backend_ok, backend_msg = check_service_endpoints_ready(
        namespace=namespace,
        service="backend",
        logger=logger,
    )
    if not backend_ok:
        raise RuntimeError(f"Cluster not ready for bench: {backend_msg}")

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
        app_port=backend_port,
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
        namespace=namespace,
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
        entry_node = (
            profile.worker_nodes[0] if profile.worker_nodes else profile.control_node
        )
        DistributedLocustSession(config).run(target=entry_node)

    return deploy_result


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BenchAttemptResult:
    """Outcome of one bench run attempt."""

    ok: bool = False
    error: str = ""
    failure: BenchFailureRecord | None = None


@dataclass(frozen=True)
class BenchStageResult:
    """Set ``ok=True`` when the Locust run completed."""

    ok: bool = False


# ---------------------------------------------------------------------------
# Attempt-level helpers
# ---------------------------------------------------------------------------

def rotate_top_level_into_attempt(
    iteration_path: Path,
    attempt_dir: Path,
) -> None:
    """Move top-level bench artifacts into ``attempts/<NNN>/`` before retry."""
    bench_dir = iteration_bench_dir(iteration_path)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name in (iteration_bench_log_path(iteration_path).name,):
        src = bench_dir / name
        if src.is_file():
            shutil.move(str(src), str(attempt_dir / name))


def build_bench_failure_record(
    *,
    iteration_path: Path,
    iteration_id: str,
    error: str,
    attempt: int | None = None,
) -> BenchFailureRecord:
    diagnostic_excerpt = collect_bench_failure_diagnostics(iteration_path)
    classify_text = "\n".join(
        part for part in (error, diagnostic_excerpt) if part and part.strip()
    )
    if not classify_text.strip():
        classify_text = read_failed_iteration_error_excerpt(iteration_path) or error
    kind = classify_bench_failure_kind(classify_text)
    if not diagnostic_excerpt:
        diagnostic_excerpt = classify_text
    return BenchFailureRecord(
        phase="bench",
        kind=kind,  # type: ignore[arg-type]
        iteration_id=iteration_id,
        attempt=attempt,
        summary=error or "benchmark run failed",
        diagnostic_excerpt=diagnostic_excerpt,
    )


def run_bench_attempt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    run_dir: Path,
    bench_users: int | None,
    bench_spawn_rate: int | None,
    bench_run_time: int | None,
    iteration_index: int | None,
    iteration_id: str,
    logger: logging.Logger,
    load_profile: str = "default",
    k8s_cluster: str = "",
    attempt_index: int = 1,
    enable_attempts: bool = False,
) -> BenchAttemptResult:
    from tasks import esc

    task_run_dir = task.get_save_dir(results_dir)
    folder_id = iteration_path.name
    perf_test_names = _performance_test_names(task)
    if not perf_test_names:
        append_k8s_skip(
            task_run_dir,
            sample,
            f"skipped iteration {folder_id}: no performance tests"
            if iteration_index
            else "skipped: no performance tests configured",
        )
        return BenchAttemptResult(ok=False, error="no performance tests configured")

    locustfile = _resolve_locustfile(task, run_dir)
    if locustfile is None:
        append_k8s_skip(task_run_dir, sample, "skipped: missing locustfile")
        return BenchAttemptResult(ok=False, error="missing locustfile")

    sample_slug = (
        f"{esc(task.model)}-{esc(task.env.id)}-"
        f"{esc(task.scenario.id)}-sample{sample}"
    )

    # Each name is a Locust user-class label from ``scenario.performance_tests``
    # (e.g. ``MixedPetstoreUser``). It only selects the CSV output prefix; the same
    # locustfile is used for every name. Scenarios with no explicit list get one
    # synthetic run named ``default``.
    for perf_test_name in perf_test_names:
        csv_prefix = locust_csv_prefix(run_dir, perf_test_name)
        logger.info(
            "running k8s bench iteration=%s perf_test=%s locustfile=%s",
            folder_id,
            perf_test_name,
            locustfile,
        )
        try:
            run_distributed_locust(
                iteration_path=iteration_path,
                run_dir=run_dir,
                sample_slug=sample_slug,
                locustfile=locustfile,
                csv_prefix=csv_prefix,
                bench_users=bench_users,
                bench_spawn_rate=bench_spawn_rate,
                bench_run_time=bench_run_time,
                load_profile=load_profile,
                k8s_cluster=k8s_cluster,
                logger=logger,
            )
        except Exception as e:
            logger.exception("k8s bench failed: %s", e, exc_info=e)
            error = str(e) or e.__class__.__name__
            failure = build_bench_failure_record(
                iteration_path=iteration_path,
                iteration_id=iteration_id,
                error=error,
                attempt=attempt_index,
            )
            from ..failure.persist import persist_bench_attempt_failure

            persist_bench_attempt_failure(
                iteration_path=iteration_path,
                attempt_index=attempt_index,
                enable_attempts=enable_attempts,
                record=failure,
                logger=logger,
            )
            return BenchAttemptResult(ok=False, error=error, failure=failure)

    from ..plots import refresh_plots_after_bench

    refresh_plots_after_bench(
        run_dir,
        experiment_root_from_iteration_path(iteration_path),
        logger=logger,
    )
    return BenchAttemptResult(ok=True)


def _performance_test_names(task: Any) -> list[str]:
    if task.scenario.performance_tests:
        return list(task.scenario.performance_tests)
    if task.scenario.locustfile:
        return ["default"]
    return []


def _resolve_locustfile(task: Any, run_dir: Path) -> Path | None:
    from load_bench.paths import locust_dir

    if not task.scenario.locustfile:
        return None
    locustfile = locust_dir(run_dir) / f"locustfile-{task.scenario.id.lower()}.py"
    locustfile.write_text(task.scenario.locustfile, encoding="utf-8")
    return locustfile


# ---------------------------------------------------------------------------
# Stage-level orchestration
# ---------------------------------------------------------------------------

def persist_successful_bench_feedback(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    cfg: RunConfig,
    logger: logging.Logger,
) -> None:
    """
    After a successful Locust run: write feedback artifacts, mark meta success,
    and append the experiment-summary Locust block.
    """
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    try:
        fb = collect_iteration_feedback(
            perf_run_dir=run_dir,
            iteration_path=iteration_path,
            logger=logger,
        )
        write_feedback(run_dir, fb)
        update_iteration_meta(
            iteration_path,
            status="success",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            summary_path = append_perf_run_block(
                sample_dir=ctx.sample_dir,
                iteration_id=plan.iteration_id,
                perf_run_dir=run_dir,
                feedback=fb,
                load_profile=cfg.load_profile,
            )
            logger.info("Updated experiment summary: %s", summary_path)
        except Exception as exc:
            logger.warning("Could not update experiment summary: %s", exc)
    except Exception as exc:
        logger.warning("Could not write iteration feedback: %s", exc)


def run_bench_stage(
    ctx: SampleContext,
    plan: IterationPlan,
    run_dir: Path,
    cfg: RunConfig,
    logger: logging.Logger,
) -> BenchStageResult:
    """Run the Locust bench for one iteration (deploy must have succeeded)."""
    iteration_path = resolve_iteration_dir(
        ctx.sample_dir, plan.iteration_id, experiment_id=ctx.experiment_id
    )
    result = run_bench_attempt(
        task=ctx.task,
        results_dir=ctx.results_dir,
        sample=ctx.sample,
        iteration_path=iteration_path,
        run_dir=run_dir,
        bench_users=cfg.bench_users,
        bench_spawn_rate=cfg.bench_spawn_rate,
        bench_run_time=cfg.bench_run_time,
        iteration_index=plan.iteration_index,
        iteration_id=plan.iteration_id,
        logger=logger,
        load_profile=cfg.load_profile,
        k8s_cluster=ctx.k8s_cluster,
        attempt_index=1,
        enable_attempts=False,
    )
    if result.ok:
        persist_successful_bench_feedback(ctx, plan, run_dir, cfg, logger)
        return BenchStageResult(ok=True)

    iteration_failure = build_bench_iteration_failure(
        iteration_path,
        iteration_id=plan.iteration_id,
        terminal_attempt=1,
        fallback=result.failure,
        logger=logger,
    )
    fail_iteration_phase(
        iteration_path=iteration_path,
        task_run_dir=ctx.task_run_dir,
        sample_dir=ctx.sample_dir,
        sample=ctx.sample,
        iteration_id=plan.iteration_id,
        kind="bench",
        logger=logger,
        iteration_failure=iteration_failure,
    )
    return BenchStageResult()


__all__ = [
    "BenchAttemptResult",
    "BenchStageResult",
    "build_bench_failure_record",
    "load_probe_deploy_result",
    "persist_successful_bench_feedback",
    "rotate_top_level_into_attempt",
    "run_bench_attempt",
    "run_bench_stage",
    "run_distributed_locust",
]

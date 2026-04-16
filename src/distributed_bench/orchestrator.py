from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import shlex
import subprocess
from dataclasses import asdict

import remote_exec
from bench_models import DistributedBenchContext, DistributedBenchPlan, RemoteConfig, host_slug
from env.base import Env

from .backend import BackendManager
from .common import ensure_docker_and_warm_ssh, phase, preclean_hosts, stage_image_to_backends
from .config import RuntimeToggles
from .database import DatabaseManager
from .load_profiles import merge_load_profile_with_overrides, resolve_load_profile
from .loadbalancer import LoadBalancerManager
from .locustrunner import LocustRunner
from .runtime import RemoteRuntime
from .system_configs import apply_system_topology_env_overrides, resolve_system_topology


def _write_run_config_snapshot(
    *,
    sample_dir: pathlib.Path,
    toggles: RuntimeToggles,
    requested_system_topology: str,
    requested_load_profile: str,
    system_topology,
    load_profile,
    effective_remote_config: RemoteConfig,
    bench_users_override: int | None,
    bench_spawn_rate_override: int | None,
    bench_run_time_override: int | None,
) -> None:
    resource_override_env_keys = [
        "BAXBENCH_BACKEND_CPUS",
        "BAXBENCH_BACKEND_MEMORY",
        "BAXBENCH_BACKEND_CPUSET",
        "BAXBENCH_BACKEND_PIDS_LIMIT",
        "BAXBENCH_BACKEND_MEMORY_SWAP",
        "BAXBENCH_DB_CPUS",
        "BAXBENCH_DB_MEMORY",
        "BAXBENCH_DB_CPUSET",
        "BAXBENCH_DB_PIDS_LIMIT",
        "BAXBENCH_DB_MEMORY_SWAP",
        "BAXBENCH_LB_CPUS",
        "BAXBENCH_LB_MEMORY",
        "BAXBENCH_LB_CPUSET",
        "BAXBENCH_LB_PIDS_LIMIT",
        "BAXBENCH_LB_MEMORY_SWAP",
        "BAXBENCH_LOAD_TASKSET_CPUS",
    ]
    resource_overrides_env = {
        k: os.environ.get(k)
        for k in resource_override_env_keys
        if os.environ.get(k) not in (None, "")
    }

    snapshot = {
        "requested_profiles": {
            "system_topology": requested_system_topology,
            "load_profile": requested_load_profile,
        },
        "resolved_system_topology": asdict(system_topology),
        "resolved_load_profile": asdict(load_profile),
        "effective_remote_config": asdict(effective_remote_config),
        "bench_cli_overrides": {
            "bench_users": bench_users_override,
            "bench_spawn_rate": bench_spawn_rate_override,
            "bench_run_time": bench_run_time_override,
        },
        "resource_overrides_env": resource_overrides_env,
        "runtime_toggles": asdict(toggles),
    }

    out_path = sample_dir / "config.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def _collect_docker_logs(ctx: DistributedBenchContext) -> None:
    logs_out = pathlib.Path(ctx.sample_dir) / "logs"

    def _collect_one(host: str, bundle_name: str, container_names: list[str]) -> None:
        remote_exec.collect_docker_logs_bundle(
            host=host,
            bundle_name=bundle_name,
            container_names=container_names,
            remote_base_dir=ctx.plan.config.remote_dir("logs", ctx.sample_slug),
            local_out_dir=logs_out,
            logger=ctx.logger,
        )

    jobs: list[tuple[str, str, list[str]]] = []
    if ctx.plan.lb_host:
        jobs.append((ctx.plan.lb_host, f"lb-{ctx.sample_slug}", [ctx.lb_container_name]))
    for host, cname in ctx.backend_container_names.items():
        jobs.append((host, f"app-{ctx.sample_slug}-{host_slug(host)}", [cname]))
    if ctx.plan.needs_db:
        jobs.append((ctx.plan.db_hosts[0], f"db-{ctx.sample_slug}", [ctx.db_container_name]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs) or 1)) as ex:
        futs = [ex.submit(_collect_one, host, bname, cnames) for (host, bname, cnames) in jobs]
        for (host, bname, _cnames), fut in zip(jobs, futs):
            try:
                fut.result()
            except Exception as exc:
                ctx.logger.warning("Failed to collect docker logs bundle %s from %s: %s", bname, host, exc)


def run_remote_bench(
    config: RemoteConfig,
    env: Env,
    sample_slug: str,
    sample_dir: pathlib.Path,
    image_cache_dir: pathlib.Path | None,
    image_id: str,
    locustfile: pathlib.Path,
    csv_prefix: pathlib.Path,
    timeout: int,
    logger: logging.Logger,
    needs_db: bool = False,
    bench_users: int | None = None,
    bench_spawn_rate: int | None = None,
    bench_run_time: int | None = None,
) -> None:
    toggles = RuntimeToggles.from_env()
    system_topology = apply_system_topology_env_overrides(resolve_system_topology(toggles.system_topology))
    effective_remote_config = system_topology.apply_to_remote_config(config)
    load_profile = merge_load_profile_with_overrides(
        resolve_load_profile(toggles.load_profile),
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
        locust_processes=toggles.locust_processes,
    )

    plan = DistributedBenchPlan.from_args(
        config=effective_remote_config,
        env=env,
        needs_db=needs_db,
        bench_users=load_profile.users,
        bench_spawn_rate=load_profile.spawn_rate,
        bench_run_time=load_profile.run_time_s,
    )

    ctx = DistributedBenchContext.create(
        plan=plan,
        sample_slug=sample_slug,
        sample_dir=sample_dir,
        image_cache_dir=image_cache_dir,
        image_id=image_id,
        locustfile=locustfile,
        csv_prefix=csv_prefix,
        timeout=timeout,
        logger=logger,
    )
    _write_run_config_snapshot(
        sample_dir=sample_dir,
        toggles=toggles,
        requested_system_topology=toggles.system_topology,
        requested_load_profile=toggles.load_profile,
        system_topology=system_topology,
        load_profile=load_profile,
        effective_remote_config=effective_remote_config,
        bench_users_override=bench_users,
        bench_spawn_rate_override=bench_spawn_rate,
        bench_run_time_override=bench_run_time,
    )
    runtime = RemoteRuntime(logger)

    logger.info(
        "Using remote hosts backends=%s db=%s lb=%s load=%s, container=%s, port=%d",
        ",".join(plan.backend_hosts),
        ",".join(plan.db_hosts) if plan.needs_db else "(none)",
        plan.lb_host or "(none)",
        ",".join(plan.load_hosts),
        ctx.container_name,
        plan.app_port,
    )
    logger.info(
        "Resolved net IPs lb=%s db=%s backends=%s",
        ctx.lb_net_host or "(none)",
        (f"{plan.db_hosts[0]}={ctx.db_net_host}" if plan.needs_db else "(none)"),
        ",".join(f"{h}={ctx.backend_net_hosts[h]}" for h in plan.backend_hosts),
    )

    backend_mgr = BackendManager(
        ctx=ctx,
        env=env,
        runtime=runtime,
        toggles=toggles,
        system_topology=system_topology,
    )
    db_mgr = (
        DatabaseManager(
            ctx=ctx,
            runtime=runtime,
            toggles=toggles,
            system_topology=system_topology,
        )
        if plan.needs_db
        else None
    )
    lb_mgr = (
        LoadBalancerManager(
            ctx=ctx,
            env=env,
            runtime=runtime,
            toggles=toggles,
            system_topology=system_topology,
        )
        if plan.lb_host
        else None
    )
    locust_runner = LocustRunner(
        ctx=ctx,
        toggles=toggles,
        load_profile=load_profile,
        system_topology=system_topology,
    )

    with phase(logger, "Remote prep", extra=f"hosts={len(ctx.involved_hosts)} ssh_multiplex={int(toggles.ssh_multiplex)}"):
        ensure_docker_and_warm_ssh(ctx)
    with phase(logger, "Preclean remote dirs"):
        preclean_hosts(
            ctx,
            keep_backends=toggles.keep_backends,
            keep_db=toggles.keep_db,
            keep_lb=toggles.keep_lb,
        )
    with phase(logger, "Stage image tar to backends", extra=f"image={image_id} backends={len(plan.backend_hosts)}"):
        stage_image_to_backends(ctx)

    if db_mgr:
        with phase(logger, "DB setup/reuse", extra=f"host={plan.db_hosts[0]} keep_db={int(toggles.keep_db)}"):
            db_mgr.setup_or_reuse()
        with phase(logger, "Configure DB connectivity", extra="direct"):
            db_mgr.configure_backend_connectivity()

    with phase(logger, "Start/reuse backends", extra=f"count={len(plan.backend_hosts)} keep_backends={int(toggles.keep_backends)}"):
        backend_mgr.start_or_reuse()
    backend_mgr.graceful_start_delay()
    backend_mgr.collect_recent_logs()

    try:
        with phase(logger, "Wait for backends ready"):
            backend_mgr.wait_ready()
        if lb_mgr is not None:
            with phase(logger, "Start/reuse LB", extra="direct"):
                with phase(logger, "LB config + start/reuse", extra=f"host={plan.lb_host} keep_lb={int(toggles.keep_lb)}"):
                    lb_mgr.setup_or_reuse()
            with phase(logger, "Wait for LB ready"):
                lb_mgr.wait_ready()

        if db_mgr:
            db_mgr.start_sampler()

        with phase(
            logger,
            "Run Locust",
            extra=(
                f"users={plan.bench_users} spawn={plan.bench_spawn_rate} "
                f"runtime={plan.locust_run_time} procs={load_profile.locust_processes}"
            ),
        ):
            if lb_mgr is not None:
                locust_runner.run(load_targets={h: lb_mgr.lb_target_for_loader(h) for h in plan.load_hosts})
            else:
                # Validated in DistributedBenchPlan.__post_init__: exactly one backend when no LB.
                only_backend = plan.backend_hosts[0]
                locust_runner.run(load_targets={h: ctx.backend_net_hosts[only_backend] for h in plan.load_hosts})

        # Access logs from DB and LB
        if db_mgr:
            try:
                with phase(logger, "Stop DB sampler + copy DB CSVs"):
                    db_mgr.stop_sampler_and_copy()
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to fetch DB metrics CSVs: %s", exc)

        if lb_mgr is not None:
            try:
                with phase(logger, "Copy LB timing access log"):
                    lb_mgr.copy_timing_access_log()
            except Exception as exc:
                logger.warning("Failed to fetch LB timing access log: %s", exc)
    finally:
        if toggles.collect_docker_logs:
            try:
                with phase(logger, "Collect docker logs bundles"):
                    _collect_docker_logs(ctx)
            except Exception as exc:
                logger.warning("Failed to collect docker logs bundles: %s", exc)

        if toggles.skip_teardown:
            logger.info(
                "Skipping all remote teardown (BAXBENCH_SKIP_TEARDOWN=1). "
                "Leaving tunnels/containers running for debugging."
            )
            return

        if lb_mgr is not None:
            lb_mgr.cleanup()
        backend_mgr.cleanup()
        if db_mgr:
            db_mgr.cleanup()

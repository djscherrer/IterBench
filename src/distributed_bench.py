import concurrent.futures
import hashlib
import logging
import os
import pathlib
import shlex
import subprocess
import threading
import time

from fabric import Connection

from db_manager import PostgresManager
from env.base import Env
from bench_models import RemoteConfig
import remote_exec
from bench_models import DistributedBenchContext, DistributedBenchPlan, host_slug


_REMOTE_LOAD_PACKAGES = ("locust", "faker", "zope.event==5")
_REMOTE_ENV_MARKER = hashlib.sha256("|".join(_REMOTE_LOAD_PACKAGES).encode("utf-8")).hexdigest()[:12]

_SSH_MULTIPLEX = os.environ.get("BAXBENCH_SSH_MULTIPLEX", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_LOG_COMMANDS = os.environ.get("BAXBENCH_LOG_COMMANDS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_COLLECT_DOCKER_LOGS = remote_exec._COLLECT_DOCKER_LOGS


def _ensure_docker_and_warm_ssh(ctx: DistributedBenchContext) -> None:
    # Rootless docker should be running on every involved host.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ensure_rootless_docker(h, ctx.logger), ctx.involved_hosts))

    # If SSH multiplexing is enabled, proactively establish ControlMaster sessions
    # so subsequent small SSH/SCP calls avoid repeated handshakes.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ssh_warmup(h, ctx.logger), ctx.involved_hosts))


def _preclean_hosts(ctx: DistributedBenchContext) -> None:
    # Best-effort pre-cleanup of per-sample directories on each host.
    # This prevents leftover artifacts from previous runs (or partial failures) from breaking setup.
    def _preclean_host(h: str) -> None:
        paths: list[str] = []
        if h in ctx.remote_app_dirs:
            paths.append(ctx.remote_app_dirs[h])
        if h == ctx.plan.lb_host:
            paths.append(ctx.plan.config.remote_dir("lb", ctx.sample_slug))
            paths.append(ctx.plan.config.remote_dir("tunnels", ctx.sample_slug))
        if h == ctx.plan.load_host:
            paths.append(ctx.remote_load_dir)
        if ctx.plan.needs_db and h == ctx.plan.db_host:
            paths.append(ctx.plan.config.remote_dir("db", ctx.sample_slug))

        if not paths:
            return
        rm_cmd = "set -euo pipefail; " + " ".join(f"rm -rf {shlex.quote(p)};" for p in paths)
        remote_exec.ssh(h, f"bash -lc {shlex.quote(rm_cmd)}", ctx.logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(_preclean_host, ctx.involved_hosts))


def _stage_image_to_backends(ctx: DistributedBenchContext) -> None:
    def _prep_backend_dir_and_tar(h: str) -> None:
        cmd = (
            "set -euo pipefail; "
            f"mkdir -p {shlex.quote(ctx.remote_app_dirs[h])}; "
            f"if [ -f {shlex.quote(ctx.remote_tars[h])} ]; then echo HAVE_TAR; else echo NEED_TAR; fi"
        )
        out = remote_exec.ssh(h, f"bash -lc {shlex.quote(cmd)}", ctx.logger)
        out.check_returncode()
        text = (out.stdout or b"").decode(errors="ignore")
        if "NEED_TAR" in text:
            remote_exec.scp_to_remote(ctx.tar_path, h, ctx.remote_tars[h], ctx.logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.plan.backend_hosts) or 1)) as ex:
        list(ex.map(_prep_backend_dir_and_tar, list(ctx.plan.backend_hosts)))


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
    """
    Distributed / multi-host version of a performance benchmark run.

    High-level flow:
    - Ensure docker is running on all involved hosts
    - Stage the app image tar on backend hosts and start one backend container per host
    - (Optional) start a Postgres container on db_host and connect backends via SSH tunnels
    - Start an nginx load balancer container on lb_host (with SSH tunnels to each backend)
    - Copy the locustfile to load_host, ensure locust venv, run locust headlessly
    - Copy result CSVs back locally; collect container logs; teardown (unless opted out)
    """

    plan = DistributedBenchPlan.from_args(
        config=config,
        env=env,
        needs_db=needs_db,
        bench_users=bench_users,
        bench_spawn_rate=bench_spawn_rate,
        bench_run_time=bench_run_time,
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

    logger.info(
        "Using remote hosts backends=%s db=%s lb=%s load=%s, container=%s, port=%d",
        ",".join(plan.backend_hosts),
        plan.db_host if plan.needs_db else "(none)",
        plan.lb_host,
        plan.load_host,
        ctx.container_name,
        plan.app_port,
    )

    _ensure_docker_and_warm_ssh(ctx)
    _preclean_hosts(ctx)
    _stage_image_to_backends(ctx)

    # if needs db, start postgres container
    if plan.needs_db:
        # Start Postgres on db_host, published on 5432 for cross-host access.
        # NOTE: this assumes backend hosts can reach db_host:5432.
        start_db_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {shlex.quote(ctx.db_container_name)} >/dev/null 2>&1 || true; "
            f"docker run -d --name {shlex.quote(ctx.db_container_name)} "
            # Bind explicitly so other hosts can reach it (rootless may otherwise bind to loopback).
            "-p 0.0.0.0:5432:5432 "
            f"-e POSTGRES_USER={PostgresManager.DEFAULT_USER} "
            f"-e POSTGRES_PASSWORD={PostgresManager.DEFAULT_PASSWORD} "
            f"-e POSTGRES_DB={PostgresManager.DEFAULT_DATABASE} "
            f"postgres:17-alpine "
            "-c listen_addresses='*' "
            "-c max_connections=300 "
            "-c shared_preload_libraries=pg_stat_statements "
            "-c pg_stat_statements.track=all "
            "-c track_io_timing=on"
        )
        start_db_cmd = f'bash -lc "{start_db_cmd}"'
        remote_exec.ssh(plan.db_host, start_db_cmd, logger)
        logger.info("Remote Postgres started")

        # Wait for DB ready
        wait_cmd = (
            f"timeout 30s bash -lc 'until docker exec {shlex.quote(ctx.db_container_name)} "
            f"pg_isready -U {PostgresManager.DEFAULT_USER}; do sleep 1; done'"
        )
        remote_exec.ssh(plan.db_host, wait_cmd, logger)

        # Enable pg_stat_statements extension (best-effort)
        sql = "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
        ext_cmd = (
            "set -euo pipefail; "
            f"docker exec {shlex.quote(ctx.db_container_name)} "
            f"psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} "
            "-v ON_ERROR_STOP=1 "
            f"-c {shlex.quote(sql)}"
        )
        remote_exec.ssh(plan.db_host, f"bash -lc {shlex.quote(ext_cmd)}", logger)

        # Backends connect to DB through host-bound SSH tunnels when they are not on db_host.
        # Containers cannot use host loopback directly, so they target the host IP.
        tunnel_jobs: list[tuple[int, str]] = []
        for idx, h in enumerate(plan.backend_hosts):
            if h == plan.db_host:
                ctx.db_host_for_backend[h] = ctx.db_net_host
                ctx.db_port_for_backend[h] = 5432
            else:
                tunnel_jobs.append((idx, h))

        def _start_db_tunnel(job: tuple[int, str]) -> tuple[str, str, int]:
            idx, h = job
            local_db_port = 15432 + idx
            pidfile = remote_exec.start_remote_ssh_tunnel(
                host=h,
                tunnel_name=f"db-{sample_slug}-{idx}",
                local_port=local_db_port,
                target_host="127.0.0.1",
                target_port=5432,
                ssh_dest=plan.db_host,
                tunnel_dir=ctx.remote_tunnel_dir,
                logger=logger,
                bind_host="0.0.0.0",
            )
            return (h, pidfile, local_db_port)

        if tunnel_jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(tunnel_jobs) or 1)) as ex:
                for h, pidfile, local_db_port in ex.map(_start_db_tunnel, tunnel_jobs):
                    ctx.active_tunnels.append((h, pidfile))
                    ctx.db_host_for_backend[h] = ctx.backend_net_hosts[h]
                    ctx.db_port_for_backend[h] = local_db_port

    def _start_backend(h: str) -> None:
        cname = ctx.backend_container_names[h]
        env_vars = ctx.env_vars_base
        if plan.needs_db:
            env_vars += (
                f"-e DB_HOST={shlex.quote(ctx.db_host_for_backend[h])} "
                f"-e DB_PORT={ctx.db_port_for_backend[h]} "
                f"-e DB_USER={PostgresManager.DEFAULT_USER} "
                f"-e DB_PASSWORD={PostgresManager.DEFAULT_PASSWORD} "
                f"-e DB_NAME={PostgresManager.DEFAULT_DATABASE} "
            )
        start_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(ctx.remote_app_dirs[h])}; "
            f"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true; "
            # Loading the image tar on every run is expensive (CPU+disk).
            # Skip it if the image is already present on this host.
            f"if docker image inspect {shlex.quote(ctx.image_id)} >/dev/null 2>&1; then "
            "  :; "
            "else "
            f"  docker load -i {shlex.quote(ctx.tar_path.name)} >/dev/null; "
            "fi; "
            f"docker run -d --name {shlex.quote(cname)} "
            f"{env_vars} "
            # Bind explicitly so other hosts can reach it (rootless may otherwise bind to loopback).
            f"-p 0.0.0.0:{plan.app_port}:{env.port}/tcp {shlex.quote(ctx.image_id)}"
        )
        remote_exec.ssh(h, f'bash -lc "{start_cmd}"', logger)

    # Start backends in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(plan.backend_hosts) or 1)) as ex:
        list(ex.map(_start_backend, list(plan.backend_hosts)))

    # Give containers a moment to start and potentially fail
    time.sleep(2)

    # Check backend logs (best-effort)
    def _fetch_backend_logs(h: str) -> tuple[str, bytes]:
        cname = ctx.backend_container_names[h]
        logs_cmd = f'bash -lc "docker logs {shlex.quote(cname)} 2>&1 | tail -n 200"'
        logs_result = remote_exec.ssh(h, logs_cmd, logger)
        return (h, logs_result.stdout or b"")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(plan.backend_hosts) or 1)) as ex:
        for h, out in ex.map(_fetch_backend_logs, list(plan.backend_hosts)):
            if out:
                logger.info("Backend logs (%s):\n%s", h, out)

    # DB sampler state (must exist for finally even if we fail early)
    remote_db_csv = ""
    remote_db_stop = ""
    remote_db_pid = ""

    try:
        # Wait for each backend to be reachable on its own host loopback.
        for h in plan.backend_hosts:
            remote_exec.wait_for_remote_http("127.0.0.1", plan.app_port, config, env, logger, probe_host=h)

        # Build LB upstream endpoints; use ssh tunnels so we don't depend on inter-host published ports.
        #
        # IMPORTANT: Nginx runs inside a Docker container on lb_host. That means `127.0.0.1` from Nginx
        # points to the container loopback, not the host loopback where the SSH tunnel binds.
        # So we bind tunnels on 0.0.0.0 and let Nginx reach them via the host's routable IP.
        lb_host_ip = ctx.lb_net_host
        if lb_host_ip.startswith("127."):
            lb_host_ip = remote_exec.resolve_remote_primary_ipv4(plan.lb_host, logger)

        # Start LB->backend tunnels in parallel.
        def _start_lb_tunnel(idx_h: tuple[int, str]) -> tuple[str, int, str]:
            idx, h = idx_h
            local_lb_port = 17001 + idx
            pidfile = remote_exec.start_remote_ssh_tunnel(
                host=plan.lb_host,
                tunnel_name=f"lb-{sample_slug}-{idx}",
                local_port=local_lb_port,
                target_host="127.0.0.1",
                target_port=plan.app_port,
                ssh_dest=h,
                tunnel_dir=ctx.remote_tunnel_dir,
                logger=logger,
                bind_host="0.0.0.0",
            )
            return (h, local_lb_port, pidfile)

        lb_upstream_endpoints: list[tuple[str, int]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(plan.backend_hosts) or 1)) as ex:
            for _h, local_lb_port, pidfile in ex.map(_start_lb_tunnel, list(enumerate(plan.backend_hosts))):
                ctx.active_tunnels.append((plan.lb_host, pidfile))
                ctx.active_lb_tunnels.append((plan.lb_host, pidfile))
                lb_upstream_endpoints.append((lb_host_ip, local_lb_port))

        # Start nginx LB on lb_host (can be the load host).
        upstream = "\n".join(
            f"        server {host}:{port};" for host, port in lb_upstream_endpoints
        )
        nginx_conf = (
            "events {}\n"
            "http {\n"
            "    upstream baxbench_upstream {\n"
            f"{upstream}\n"
            "    }\n"
            "    server {\n"
            "        listen 80;\n"
            "        location / {\n"
            "            proxy_pass http://baxbench_upstream;\n"
            "            proxy_http_version 1.1;\n"
            "            proxy_set_header Connection \"\";\n"
            "            proxy_set_header Host $host;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        remote_lb_dir = config.remote_dir("lb", sample_slug)
        remote_nginx_conf = f"{remote_lb_dir}/nginx.conf"
        remote_exec.ssh(plan.lb_host, f"mkdir -p {shlex.quote(remote_lb_dir)}", logger).check_returncode()
        # Write nginx.conf on lb host
        write_cmd = f"cat > {shlex.quote(remote_nginx_conf)} <<'EOF'\n{nginx_conf}\nEOF\n"
        remote_exec.ssh(plan.lb_host, f"bash -lc {shlex.quote(write_cmd)}", logger)

        lb_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {shlex.quote(ctx.lb_container_name)} >/dev/null 2>&1 || true; "
            f"docker run -d --name {shlex.quote(ctx.lb_container_name)} "
            # Publish the LB on app_port. Rootless Docker often does not support `--network host`.
            f"-p 0.0.0.0:{plan.app_port}:80 "
            f"-v {shlex.quote(remote_nginx_conf)}:/etc/nginx/nginx.conf:ro "
            "nginx:1.27-alpine"
        )
        remote_exec.ssh(plan.lb_host, f'bash -lc "{lb_cmd}"', logger)

        # Wait for LB to be reachable from load host.
        lb_target_for_load = "127.0.0.1" if plan.load_host == plan.lb_host else ctx.lb_net_host
        remote_exec.wait_for_remote_http(lb_target_for_load, plan.app_port, config, env, logger)

        # Start DB metrics sampler on DB host (best-effort) to produce db_performance.csv.
        # This matches the local db_performance.csv schema used by plotting.
        if plan.needs_db:
            remote_db_dir = config.remote_dir("db", sample_slug)
            remote_db_csv = f"{remote_db_dir}/db_performance.csv"
            remote_db_wait_csv = f"{remote_db_dir}/db_queue.csv"
            remote_db_stop = f"{remote_db_dir}/STOP"
            remote_db_pid = f"{remote_db_dir}/sampler.pid"
            remote_exec.ssh(plan.db_host, f"mkdir -p {shlex.quote(remote_db_dir)}", logger).check_returncode()
            sampler_cmd = (
                "set -euo pipefail; "
                f"rm -f {shlex.quote(remote_db_stop)} {shlex.quote(remote_db_pid)}; "
                f"echo \"ts,numbackends,xact_commit,xact_rollback,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time_ms,blk_write_time_ms,stmt_calls,stmt_total_exec_time_ms\" > {shlex.quote(remote_db_csv)}; "
                f"echo \"ts,total_conns,active_conns,waiting_conns,lock_waiting_conns,idle_in_tx_conns\" > {shlex.quote(remote_db_wait_csv)}; "
                "(\n"
                f"  while [ ! -f {shlex.quote(remote_db_stop)} ]; do\n"
                "    ts=$(date +%s);\n"
                f"    dbrow=$(docker exec {shlex.quote(ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT numbackends,xact_commit,xact_rollback,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time,blk_write_time FROM pg_stat_database WHERE datname = current_database();\" | tail -n 1 | tr -d '\\r');\n"
                f"    stmtrow=$(docker exec {shlex.quote(ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT COALESCE(SUM(calls),0),COALESCE(SUM(total_exec_time),0) FROM pg_stat_statements;\" | tail -n 1 | tr -d '\\r');\n"
                f"    qrow=$(docker exec {shlex.quote(ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE state='active') AS active, COUNT(*) FILTER (WHERE wait_event_type IS NOT NULL) AS waiting, COUNT(*) FILTER (WHERE wait_event_type='Lock') AS lock_waiting, COUNT(*) FILTER (WHERE state='idle in transaction') AS idle_in_tx FROM pg_stat_activity WHERE datname=current_database();\" | tail -n 1 | tr -d '\\r');\n"
                f"    echo \"${{ts}}.${{RANDOM}},${{dbrow}},${{stmtrow}}\" >> {shlex.quote(remote_db_csv)};\n"
                f"    echo \"${{ts}}.${{RANDOM}},${{qrow}}\" >> {shlex.quote(remote_db_wait_csv)};\n"
                "    sleep 1;\n"
                "  done\n"
                ") >/dev/null 2>&1 & echo $! > "
                f"{shlex.quote(remote_db_pid)}"
            )
            remote_exec.ssh(plan.db_host, f"bash -lc {shlex.quote(sampler_cmd)}", logger)

        remote_exec.ssh(plan.load_host, f"mkdir -p {shlex.quote(ctx.remote_load_dir)}", logger)
        remote_locustfile = f"{ctx.remote_load_dir}/{locustfile.name}"
        remote_exec.scp_to_remote(locustfile, plan.load_host, remote_locustfile, logger)

        # Prepare performance + queue logging threads (LB + each backend + DB host).
        metrics_capture_stop_event = threading.Event()
        perf_threads: list[threading.Thread] = []
        queue_threads: list[threading.Thread] = []
        perf_hosts = list(plan.backend_hosts) + ([plan.db_host] if plan.needs_db else []) + [plan.lb_host]
        # De-dupe while preserving order
        seen: set[str] = set()
        perf_hosts = [h for h in perf_hosts if not (h in seen or seen.add(h))]

        for h in perf_hosts:
            host_stats_dir = sample_dir / "stats" / host_slug(h)
            host_stats_dir.mkdir(parents=True, exist_ok=True)
            out_csv = host_stats_dir / "host_performance.csv"
            t = threading.Thread(
                target=remote_exec.capture_host_performance,
                args=(sample_dir, h, logger, metrics_capture_stop_event),
                kwargs={"out_csv": out_csv, "interval": 5},
                daemon=True,
            )
            perf_threads.append(t)

            ports: list[int] = []
            if h in plan.backend_hosts or h == plan.lb_host:
                ports.append(int(plan.app_port))
            if plan.needs_db and h == plan.db_host:
                ports.append(5432)
            if ports:
                q_csv = host_stats_dir / "socket_queue.csv"
                qt = threading.Thread(
                    target=remote_exec.capture_socket_queues,
                    args=(sample_dir, h, logger, metrics_capture_stop_event),
                    kwargs={"ports": ports, "out_csv": q_csv, "interval": 5},
                    daemon=True,
                )
                queue_threads.append(qt)
        connection = Connection(plan.load_host)

        locust_bin = remote_exec.ensure_remote_python_env(plan.load_host, ctx.remote_env_dir, logger)
        remote_csv_prefix = f"{ctx.remote_load_dir}/{csv_prefix.name}"
        locust_cmd = (
            "set -euo pipefail; "
            f"cd {shlex.quote(ctx.remote_load_dir)}; "
            f"{locust_bin} --headless --locustfile {shlex.quote(locustfile.name)} "
            f"--host http://{lb_target_for_load}:{plan.app_port} "
            f"--users {plan.bench_users} "
            f"--spawn-rate {plan.bench_spawn_rate} "
            f"--run-time {plan.locust_run_time} "
            f"--csv {shlex.quote(csv_prefix.name)} "
            "--csv-full-history "
            "--only-summary "
        )

        for t in perf_threads:
            t.start()
        for t in queue_threads:
            t.start()
        locust_proc = connection.run(locust_cmd, hide=True, warn=True)
        metrics_capture_stop_event.set()
        for t in perf_threads:
            t.join()
        for t in queue_threads:
            t.join()

        logger.info("Locust output:\n%s", locust_proc)
        connection.close()

        for suffix in ("_stats_history.csv", "_stats.csv", "_failures.csv", "_exceptions.csv"):
            remote_csv = f"{remote_csv_prefix}{suffix}"
            local_csv = pathlib.Path(f"{csv_prefix}{suffix}")
            try:
                remote_exec.scp_from_remote(plan.load_host, remote_csv, local_csv, logger)
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to copy remote CSV %s: %s", remote_csv, exc)

        # Stop DB sampler and fetch db_performance.csv + db_queue.csv
        if plan.needs_db and remote_db_csv:
            try:
                db_stats_dir = sample_dir / "stats" / host_slug(plan.db_host)
                db_stats_dir.mkdir(parents=True, exist_ok=True)
                remote_exec.ssh(plan.db_host, f"bash -lc \"touch {shlex.quote(remote_db_stop)} || true\"", logger)
                # Best-effort wait
                remote_exec.ssh(
                    plan.db_host,
                    f"bash -lc \"if [ -f {shlex.quote(remote_db_pid)} ]; then kill -0 $(cat {shlex.quote(remote_db_pid)}) >/dev/null 2>&1 || true; fi\"",
                    logger,
                )
                remote_exec.scp_from_remote(
                    plan.db_host,
                    remote_db_csv,
                    (db_stats_dir / "db_performance.csv"),
                    logger,
                )
                if remote_db_wait_csv:
                    remote_exec.scp_from_remote(
                        plan.db_host,
                        remote_db_wait_csv,
                        (db_stats_dir / "db_queue.csv"),
                        logger,
                    )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to fetch DB metrics CSVs: %s", exc)
    finally:
        # Collect container logs into sample_dir/logs (best-effort).
        # This runs even if teardown is skipped to preserve debugging evidence.
        if _COLLECT_DOCKER_LOGS:
            try:
                logs_out = pathlib.Path(sample_dir) / "logs"
                # LB bundle
                remote_exec.collect_docker_logs_bundle(
                    host=plan.lb_host,
                    bundle_name=f"lb-{sample_slug}",
                    container_names=[ctx.lb_container_name],
                    remote_base_dir=config.remote_dir("logs", sample_slug),
                    local_out_dir=logs_out,
                    logger=logger,
                )
                # Backend bundles (one per backend host)
                for h, cname in ctx.backend_container_names.items():
                    remote_exec.collect_docker_logs_bundle(
                        host=h,
                        bundle_name=f"app-{sample_slug}-{host_slug(h)}",
                        container_names=[cname],
                        remote_base_dir=config.remote_dir("logs", sample_slug),
                        local_out_dir=logs_out,
                        logger=logger,
                    )
                # DB bundle
                if plan.needs_db:
                    remote_exec.collect_docker_logs_bundle(
                        host=plan.db_host,
                        bundle_name=f"db-{sample_slug}",
                        container_names=[ctx.db_container_name],
                        remote_base_dir=config.remote_dir("logs", sample_slug),
                        local_out_dir=logs_out,
                        logger=logger,
                    )
            except Exception as exc:
                logger.warning("Failed to collect docker logs bundles: %s", exc)

        # Optional: skip *all* teardown to enable post-run debugging on the remote hosts.
        # This intentionally leaves SSH tunnels, containers (LB/backends/DB), and remote dirs in place.
        # TODO: remove this once we passed the debug phase
        skip_teardown = (
            os.environ.get("BAXBENCH_SKIP_TEARDOWN", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if skip_teardown:
            logger.info(
                "Skipping all remote teardown (BAXBENCH_SKIP_TEARDOWN=1). "
                "Leaving tunnels/containers running for debugging."
            )
            return

        # Cleanup ssh tunnels.
        for tunnel_host, pidfile in ctx.active_tunnels:
            try:
                remote_exec.stop_remote_ssh_tunnel(tunnel_host, pidfile, logger)
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup tunnel %s on %s: %s", pidfile, tunnel_host, exc)

        # Cleanup LB
        keep_lb = (os.environ.get("BAXBENCH_KEEP_LB", "").strip().lower() in ("1", "true", "yes", "on"))
        if keep_lb:
            logger.info(
                "Keeping load balancer container %s on %s (BAXBENCH_KEEP_LB=1)",
                ctx.lb_container_name,
                plan.lb_host,
            )
        else:
            try:
                remote_exec.ssh(
                    plan.lb_host,
                    f"bash -lc \"docker rm -f {shlex.quote(ctx.lb_container_name)} >/dev/null 2>&1 || true\"",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup LB container: %s", exc)

        # Cleanup backends
        for h, cname in ctx.backend_container_names.items():
            try:
                remote_exec.ssh(
                    h,
                    f"bash -lc \"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true\"",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup backend %s: %s", h, exc)

        # Cleanup DB
        if plan.needs_db:
            try:
                # Stop DB sampler if still running
                if remote_db_stop:
                    remote_exec.ssh(plan.db_host, f"bash -lc \"touch {shlex.quote(remote_db_stop)} || true\"", logger)
                remote_exec.ssh(
                    plan.db_host,
                    f"bash -lc \"docker rm -f {shlex.quote(ctx.db_container_name)} >/dev/null 2>&1 || true\"",
                    logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to cleanup DB container: %s", exc)


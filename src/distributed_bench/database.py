from __future__ import annotations

import concurrent.futures
import shlex
from dataclasses import dataclass

import remote_exec
from bench_models import DistributedBenchContext, host_slug
from db_manager import PostgresManager

from .config import RuntimeToggles
from .runtime import RemoteRuntime
from .system_configs import SystemTopology

_REUSE_TRUNCATE_EXCLUDE_TABLES = (
    "alembic_version",
    "django_migrations",
    "schema_migrations",
    "flyway_schema_history",
    "knex_migrations",
    "knex_migrations_lock",
    "liquibase_databasechangelog",
    "liquibase_databasechangeloglock",
)


def sql_reset_db_data_for_container_reuse() -> str:
    exclude = ", ".join(repr(t) for t in _REUSE_TRUNCATE_EXCLUDE_TABLES)
    return f"""
DO $reset_stats$
BEGIN
  PERFORM pg_stat_statements_reset();
EXCEPTION
  WHEN OTHERS THEN
    IF SQLSTATE = '42883' THEN
      NULL;
    ELSE
      RAISE;
    END IF;
END
$reset_stats$;

DO $truncate_public$
DECLARE
  stmt text;
BEGIN
  SELECT 'TRUNCATE TABLE ' || string_agg(format('%I.%I', n.nspname, c.relname), ', ')
         || ' RESTART IDENTITY CASCADE'
  INTO stmt
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public'
    AND c.relkind = 'r'
    AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.objid = c.oid AND d.deptype = 'e'
    )
    AND lower(c.relname) NOT IN ({exclude});
  IF stmt IS NOT NULL THEN
    EXECUTE stmt;
  END IF;
END
$truncate_public$;
"""


@dataclass
class DbSamplerState:
    remote_db_csv: str = ""
    remote_db_wait_csv: str = ""
    remote_db_wait_events_csv: str = ""
    remote_db_stop: str = ""
    remote_db_pid: str = ""


class DatabaseManager:
    def __init__(
        self,
        *,
        ctx: DistributedBenchContext,
        runtime: RemoteRuntime,
        toggles: RuntimeToggles,
        system_topology: SystemTopology,
    ):
        self.ctx = ctx
        self.runtime = runtime
        self.toggles = toggles
        self.system_topology = system_topology
        self.plan = ctx.plan
        self.logger = ctx.logger
        self.sampler = DbSamplerState()

    def setup_or_reuse(self) -> None:
        db_labels = {"baxbench.sample": self.ctx.sample_slug, "baxbench.role": "db"}
        existing_db = self.runtime.docker_ps_id(self.plan.db_host, labels=db_labels) if self.toggles.keep_db else ""
        if not existing_db:
            self.runtime.docker_rm_by_labels(self.plan.db_host, labels=db_labels)

        start_db_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {shlex.quote(self.ctx.db_container_name)} >/dev/null 2>&1 || true; "
            f"docker run -d --name {shlex.quote(self.ctx.db_container_name)} "
            + " ".join(f"--label {shlex.quote(k + '=' + v)}" for k, v in db_labels.items())
            + " "
            f"{self.system_topology.db_resources.docker_run_flags()} "
            "-p 0.0.0.0:5432:5432 "
            f"-e POSTGRES_USER={PostgresManager.DEFAULT_USER} "
            f"-e POSTGRES_PASSWORD={PostgresManager.DEFAULT_PASSWORD} "
            f"-e POSTGRES_DB={PostgresManager.DEFAULT_DATABASE} "
            "postgres:17-alpine "
            "-c listen_addresses='*' "
            "-c max_connections=300 "
            "-c shared_preload_libraries=pg_stat_statements "
            "-c pg_stat_statements.track=all "
            "-c track_io_timing=on"
        )
        if existing_db:
            self.logger.info("Reusing existing Postgres container on %s (BAXBENCH_KEEP_DB=1)", self.plan.db_host)
            reused_name = self.runtime.docker_ps_name(self.plan.db_host, labels=db_labels)
            if reused_name:
                self.ctx.db_container_name = reused_name
        else:
            remote_exec.ssh(self.plan.db_host, f'bash -lc "{start_db_cmd}"', self.logger)
            self.logger.info("Remote Postgres started")

        wait_cmd = (
            f"timeout 30s bash -lc 'until docker exec {shlex.quote(self.ctx.db_container_name)} "
            f"pg_isready -U {PostgresManager.DEFAULT_USER}; do sleep 1; done'"
        )
        remote_exec.ssh(self.plan.db_host, wait_cmd, self.logger)

        ext_sql = "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
        ext_cmd = (
            "set -euo pipefail; "
            f"docker exec {shlex.quote(self.ctx.db_container_name)} "
            f"psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} "
            "-v ON_ERROR_STOP=1 "
            f"-c {shlex.quote(ext_sql)}"
        )
        remote_exec.ssh(self.plan.db_host, f"bash -lc {shlex.quote(ext_cmd)}", self.logger)

        if existing_db and self.toggles.wipe_db_on_reuse:
            wipe_sql = sql_reset_db_data_for_container_reuse()
            wipe_cmd = (
                "set -euo pipefail; "
                f"docker exec {shlex.quote(self.ctx.db_container_name)} "
                f"psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} "
                "-v ON_ERROR_STOP=1 "
                f"-c {shlex.quote(wipe_sql)}"
            )
            remote_exec.ssh(self.plan.db_host, f"bash -lc {shlex.quote(wipe_cmd)}", self.logger)
            self.logger.info(
                "Reset DB application data for reuse: truncated public tables "
                "(migration metadata preserved), pg_stat_statements reset "
                "(BAXBENCH_WIPE_DB_ON_REUSE=1)"
            )

    def configure_backend_connectivity(self) -> None:
        tunnel_jobs: list[tuple[int, str]] = []
        for idx, host in enumerate(self.plan.backend_hosts):
            if host == self.plan.db_host:
                self.ctx.db_host_for_backend[host] = self.ctx.db_net_host
                self.ctx.db_port_for_backend[host] = 5432
            else:
                tunnel_jobs.append((idx, host))

        def _start_db_tunnel(job: tuple[int, str]) -> tuple[str, str, int]:
            _idx, host = job
            local_db_port = self.runtime.stable_port(15432, f"db:{self.ctx.sample_slug}:{host}:{self.plan.db_host}")
            tunnel_name = f"db-{self.ctx.sample_slug}-{host_slug(host)}"
            tunnel_fn = remote_exec.ensure_remote_ssh_tunnel if self.toggles.keep_tunnels else remote_exec.start_remote_ssh_tunnel
            pidfile = tunnel_fn(
                host=host,
                tunnel_name=tunnel_name,
                local_port=local_db_port,
                target_host="127.0.0.1",
                target_port=5432,
                ssh_dest=self.plan.db_host,
                tunnel_dir=self.ctx.remote_tunnel_dir,
                logger=self.logger,
                bind_host="0.0.0.0",
            )
            return (host, pidfile, local_db_port)

        if not tunnel_jobs:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(tunnel_jobs) or 1)) as ex:
            for host, pidfile, local_db_port in ex.map(_start_db_tunnel, tunnel_jobs):
                self.ctx.active_tunnels.append((host, pidfile))
                self.ctx.db_host_for_backend[host] = self.ctx.backend_net_hosts[host]
                self.ctx.db_port_for_backend[host] = local_db_port

    def start_sampler(self) -> None:
        remote_db_dir = self.plan.config.remote_dir("db", self.ctx.sample_slug)
        self.sampler.remote_db_csv = f"{remote_db_dir}/db_performance.csv"
        self.sampler.remote_db_wait_csv = f"{remote_db_dir}/db_queue.csv"
        self.sampler.remote_db_wait_events_csv = f"{remote_db_dir}/db_wait_events.csv"
        self.sampler.remote_db_stop = f"{remote_db_dir}/STOP"
        self.sampler.remote_db_pid = f"{remote_db_dir}/sampler.pid"
        remote_exec.ssh(self.plan.db_host, f"mkdir -p {shlex.quote(remote_db_dir)}", self.logger).check_returncode()
        sampler_cmd = (
            "set -euo pipefail; "
            f"rm -f {shlex.quote(self.sampler.remote_db_stop)} {shlex.quote(self.sampler.remote_db_pid)}; "
            f"echo \"ts,numbackends,xact_commit,xact_rollback,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time_ms,blk_write_time_ms,stmt_calls,stmt_total_exec_time_ms\" > {shlex.quote(self.sampler.remote_db_csv)}; "
            f"echo \"ts,total_conns,active_conns,waiting_conns,lock_waiting_conns,idle_in_tx_conns\" > {shlex.quote(self.sampler.remote_db_wait_csv)}; "
            f"echo \"ts,wait_event_type,wait_event,count\" > {shlex.quote(self.sampler.remote_db_wait_events_csv)}; "
            "(\n"
            f"  while [ ! -f {shlex.quote(self.sampler.remote_db_stop)} ]; do\n"
            "    ts=$(date +%s);\n"
            f"    dbrow=$(docker exec {shlex.quote(self.ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT numbackends,xact_commit,xact_rollback,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time,blk_write_time FROM pg_stat_database WHERE datname = current_database();\" 2>/dev/null | tail -n 1 | tr -d '\\r' || true);\n"
            f"    stmtrow=$(docker exec {shlex.quote(self.ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT COALESCE(SUM(calls),0),COALESCE(SUM(total_exec_time),0) FROM pg_stat_statements;\" 2>/dev/null | tail -n 1 | tr -d '\\r' || true);\n"
            f"    qrow=$(docker exec {shlex.quote(self.ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE state='active') AS active, COUNT(*) FILTER (WHERE wait_event_type IS NOT NULL) AS waiting, COUNT(*) FILTER (WHERE wait_event_type='Lock') AS lock_waiting, COUNT(*) FILTER (WHERE state='idle in transaction') AS idle_in_tx FROM pg_stat_activity WHERE datname=current_database();\" 2>/dev/null | tail -n 1 | tr -d '\\r' || true);\n"
            f"    waitrows=$(docker exec {shlex.quote(self.ctx.db_container_name)} psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} -q -t -A -F ',' -c \"SELECT COALESCE(wait_event_type,'NONE') AS wait_event_type, COALESCE(wait_event,'NONE') AS wait_event, COUNT(*)::int AS count FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type IS NOT NULL GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;\" 2>/dev/null | tr -d '\\r' || true);\n"
            "    if [ -z \"${dbrow}\" ]; then dbrow=\",,,,,,,,,,,\"; fi;\n"
            "    if [ -z \"${stmtrow}\" ]; then stmtrow=\"0,0\"; fi;\n"
            "    if [ -z \"${qrow}\" ]; then qrow=\"0,0,0,0,0\"; fi;\n"
            f"    echo \"${{ts}}.${{RANDOM}},${{dbrow}},${{stmtrow}}\" >> {shlex.quote(self.sampler.remote_db_csv)};\n"
            f"    echo \"${{ts}}.${{RANDOM}},${{qrow}}\" >> {shlex.quote(self.sampler.remote_db_wait_csv)};\n"
            "    if [ -n \"${waitrows}\" ]; then\n"
            "      if [ $((ts % 5)) -eq 0 ]; then\n"
            f"        while IFS= read -r line; do echo \"${{ts}}.${{RANDOM}},${{line}}\" >> {shlex.quote(self.sampler.remote_db_wait_events_csv)}; done <<< \"${{waitrows}}\";\n"
            "      fi\n"
            "    fi\n"
            "    sleep 1;\n"
            "  done\n"
            ") >/dev/null 2>&1 & echo $! > "
            f"{shlex.quote(self.sampler.remote_db_pid)}"
        )
        remote_exec.ssh(self.plan.db_host, f"bash -lc {shlex.quote(sampler_cmd)}", self.logger)

    def stop_sampler_and_copy(self) -> None:
        if not self.sampler.remote_db_csv:
            return
        db_stats_dir = self.ctx.sample_dir / "stats" / host_slug(self.plan.db_host)
        db_stats_dir.mkdir(parents=True, exist_ok=True)
        remote_exec.ssh(
            self.plan.db_host,
            f"bash -lc \"touch {shlex.quote(self.sampler.remote_db_stop)} || true\"",
            self.logger,
        )
        remote_exec.ssh(
            self.plan.db_host,
            f"bash -lc \"if [ -f {shlex.quote(self.sampler.remote_db_pid)} ]; then kill -0 $(cat {shlex.quote(self.sampler.remote_db_pid)}) >/dev/null 2>&1 || true; fi\"",
            self.logger,
        )
        remote_exec.scp_from_remote(
            self.plan.db_host,
            self.sampler.remote_db_csv,
            (db_stats_dir / "db_performance.csv"),
            self.logger,
        )
        if self.sampler.remote_db_wait_csv:
            remote_exec.scp_from_remote(
                self.plan.db_host,
                self.sampler.remote_db_wait_csv,
                (db_stats_dir / "db_queue.csv"),
                self.logger,
            )
        if self.sampler.remote_db_wait_events_csv:
            remote_exec.scp_from_remote(
                self.plan.db_host,
                self.sampler.remote_db_wait_events_csv,
                (db_stats_dir / "db_wait_events.csv"),
                self.logger,
            )

    def cleanup(self) -> None:
        if self.toggles.keep_db:
            self.logger.info("Keeping DB container on %s (BAXBENCH_KEEP_DB=1)", self.plan.db_host)
            return
        try:
            if self.sampler.remote_db_stop:
                remote_exec.ssh(
                    self.plan.db_host,
                    f"bash -lc \"touch {shlex.quote(self.sampler.remote_db_stop)} || true\"",
                    self.logger,
                )
            remote_exec.ssh(
                self.plan.db_host,
                f"bash -lc \"docker rm -f {shlex.quote(self.ctx.db_container_name)} >/dev/null 2>&1 || true\"",
                self.logger,
            )
        except Exception as exc:
            self.logger.warning("Failed to cleanup DB container: %s", exc)

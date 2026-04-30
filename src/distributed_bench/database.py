from __future__ import annotations

import shlex
from dataclasses import dataclass

import remote_exec
from bench_models import DistributedBenchContext, host_slug
from db_manager import PostgresManager

from .config import RuntimeToggles
from .runtime import RemoteRuntime
from .system_configs import SystemTopology


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

    def setup(self) -> None:
        db_host = self.plan.db_hosts[0]
        db_labels = {"baxbench.sample": self.ctx.sample_slug, "baxbench.role": "db"}
        # Always start a fresh DB container for reproducibility and to avoid cross-sample port conflicts.
        # (Debugging uses BAXBENCH_SKIP_TEARDOWN=1 to keep containers around.)
        self.runtime.docker_rm_by_labels(db_host, labels={"baxbench.role": "db"})

        dbn_q = shlex.quote(self.ctx.db_container_name)
        db_res = self.system_topology.db_resources
        pin_db = db_res.bash_apply_taskset_to_container(dbn_q)
        start_db_cmd = (
            "set -euo pipefail; "
            f"docker rm -f {dbn_q} >/dev/null 2>&1 || true; "
            f"docker run -d --name {dbn_q} "
            + " ".join(f"--label {shlex.quote(k + '=' + v)}" for k, v in db_labels.items())
            + " "
            f"{db_res.docker_run_flags()} "
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
        if pin_db:
            start_db_cmd += f"; {pin_db}"
        # Use shlex.quote() so hostpin fragments containing quotes don't break the script.
        out = remote_exec.ssh(db_host, f"bash -lc {shlex.quote(start_db_cmd)}", self.logger)
        out.check_returncode()
        self.logger.info("Remote Postgres started")

        wait_cmd = (
            f"timeout 30s bash -lc 'until docker exec {shlex.quote(self.ctx.db_container_name)} "
            f"pg_isready -U {PostgresManager.DEFAULT_USER}; do sleep 1; done'"
        )
        remote_exec.ssh(db_host, wait_cmd, self.logger).check_returncode()

        # Verification: record the effective CPU affinity after pinning.
        if db_res.taskset_cpus:
            verify_cmd = (
                "set -euo pipefail; "
                "docker_sock=\"/run/user/$(id -u)/docker.sock\"; "
                "if [[ -z \"${DOCKER_HOST:-}\" && -S \"$docker_sock\" ]]; then export DOCKER_HOST=\"unix://$docker_sock\"; fi; "
                f"pid=$(docker inspect -f '{{{{.State.Pid}}}}' {dbn_q} 2>/dev/null || echo ''); "
                f"echo \"PINVERIFY role=db host={db_host} container={self.ctx.db_container_name} expected={db_res.taskset_cpus} pid=${{pid}}\"; "
                "if [[ -n \"${pid:-}\" && \"${pid:-}\" != 0 ]]; then "
                f"  _out=$(taskset -apc {shlex.quote(db_res.taskset_cpus)} \"$pid\" 2>&1) && _rc=0 || _rc=$?; "
                "  echo \"PINAPPLY rc=${_rc} out=${_out}\"; "
                "  taskset -pc \"$pid\" 2>&1 || true; "
                "  grep -E '^Cpus_allowed_list:' \"/proc/$pid/status\" 2>&1 || true; "
                "fi"
            )
            vout = remote_exec.ssh(db_host, f"bash -lc {shlex.quote(verify_cmd)}", self.logger)
            txt = (vout.stdout or b"").decode(errors="ignore").strip()
            if txt:
                self.logger.info("%s", txt)

        ext_sql = "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
        ext_cmd = (
            "set -euo pipefail; "
            f"docker exec {shlex.quote(self.ctx.db_container_name)} "
            f"psql -U {PostgresManager.DEFAULT_USER} -d {PostgresManager.DEFAULT_DATABASE} "
            "-v ON_ERROR_STOP=1 "
            f"-c {shlex.quote(ext_sql)}"
        )
        remote_exec.ssh(db_host, f"bash -lc {shlex.quote(ext_cmd)}", self.logger)

    def configure_backend_connectivity(self) -> None:
        # Direct connectivity: every backend talks directly to the DB host over the network.
        # (No per-backend SSH port-forward tunnels.)
        for host in self.plan.backend_hosts:
            self.ctx.db_host_for_backend[host] = self.ctx.db_net_host
            self.ctx.db_port_for_backend[host] = 5432

    def start_sampler(self) -> None:
        db_host = self.plan.db_hosts[0]
        remote_db_dir = self.plan.config.remote_dir("db", self.ctx.sample_slug)
        self.sampler.remote_db_csv = f"{remote_db_dir}/db_performance.csv"
        self.sampler.remote_db_wait_csv = f"{remote_db_dir}/db_queue.csv"
        self.sampler.remote_db_wait_events_csv = f"{remote_db_dir}/db_wait_events.csv"
        self.sampler.remote_db_stop = f"{remote_db_dir}/STOP"
        self.sampler.remote_db_pid = f"{remote_db_dir}/sampler.pid"
        remote_exec.ssh(db_host, f"mkdir -p {shlex.quote(remote_db_dir)}", self.logger).check_returncode()
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
        remote_exec.ssh(db_host, f"bash -lc {shlex.quote(sampler_cmd)}", self.logger)

    def stop_sampler_and_copy(self) -> None:
        if not self.sampler.remote_db_csv:
            return
        db_host = self.plan.db_hosts[0]
        db_stats_dir = self.ctx.sample_dir / "stats" / host_slug(db_host)
        db_stats_dir.mkdir(parents=True, exist_ok=True)
        remote_exec.ssh(
            db_host,
            f"bash -lc \"touch {shlex.quote(self.sampler.remote_db_stop)} || true\"",
            self.logger,
        )
        remote_exec.ssh(
            db_host,
            f"bash -lc \"if [ -f {shlex.quote(self.sampler.remote_db_pid)} ]; then kill -0 $(cat {shlex.quote(self.sampler.remote_db_pid)}) >/dev/null 2>&1 || true; fi\"",
            self.logger,
        )
        remote_exec.scp_from_remote(
            db_host,
            self.sampler.remote_db_csv,
            (db_stats_dir / "db_performance.csv"),
            self.logger,
        )
        if self.sampler.remote_db_wait_csv:
            remote_exec.scp_from_remote(
                db_host,
                self.sampler.remote_db_wait_csv,
                (db_stats_dir / "db_queue.csv"),
                self.logger,
            )
        if self.sampler.remote_db_wait_events_csv:
            remote_exec.scp_from_remote(
                db_host,
                self.sampler.remote_db_wait_events_csv,
                (db_stats_dir / "db_wait_events.csv"),
                self.logger,
            )

    def cleanup(self) -> None:
        db_host = self.plan.db_hosts[0]
        try:
            if self.sampler.remote_db_stop:
                remote_exec.ssh(
                    db_host,
                    f"bash -lc \"touch {shlex.quote(self.sampler.remote_db_stop)} || true\"",
                    self.logger,
                )
            # Be robust to container name/label drift across retries: remove any baxbench-managed DB
            # container that might still be running and holding port 5432.
            self.runtime.docker_rm_by_labels(db_host, labels={"baxbench.role": "db"})
        except Exception as exc:
            self.logger.warning("Failed to cleanup DB container: %s", exc)

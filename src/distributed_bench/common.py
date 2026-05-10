from __future__ import annotations

import concurrent.futures
import logging
import os
import shlex
import time
from contextlib import contextmanager

import remote_exec
from bench_models import DistributedBenchContext


@contextmanager
def phase(logger: logging.Logger, name: str, *, extra: str = ""):
    t0 = time.time()
    line = f"==> {name}" + (f" ({extra})" if extra else "")
    logger.info(line)
    try:
        yield
    finally:
        dt = time.time() - t0
        logger.info("<== %s (%.2fs)", name, dt)


def ensure_docker_and_warm_ssh(ctx: DistributedBenchContext) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ensure_docker_access(h, ctx.logger), ctx.involved_hosts))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ssh_warmup(h, ctx.logger), ctx.involved_hosts))
    # The load generator hosts (Locust master/workers) need python tooling (venv + pip).
    load_hosts = [ctx.plan.load_master, *list(ctx.plan.load_workers)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(load_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ensure_remote_python_tooling(h, ctx.logger), load_hosts))


def preclean_hosts(
    ctx: DistributedBenchContext,
) -> None:
    def _preclean_host(host: str) -> None:
        remote_exec.cleanup_remote_docker_host(host, ctx.logger)

        # Optional: wipe the entire remote base dir to prevent /tmp from filling up
        # across many runs (this removes *all* baxbench artifacts under remote_base_dir).
        if os.environ.get("BAXBENCH_WIPE_REMOTE_BASE_DIR", "").strip().lower() in ("1", "true", "yes", "on"):
            base = ctx.plan.config.remote_base_dir.rstrip("/") or "."
            wipe_cmd = (
                "set -euo pipefail; "
                f"rm -rf {shlex.quote(base)}; "
                f"mkdir -p {shlex.quote(base)}"
            )
            remote_exec.ssh(host, f"bash -lc {shlex.quote(wipe_cmd)}", ctx.logger).check_returncode()

        paths: list[str] = []
        if host in ctx.remote_app_dirs:
            paths.append(ctx.remote_app_dirs[host])
        if ctx.plan.lb_host and host == ctx.plan.lb_host:
            paths.append(ctx.plan.config.remote_dir("lb", ctx.sample_slug))
        if host == ctx.plan.load_master or host in ctx.plan.load_workers:
            paths.append(ctx.remote_load_dir)
        if ctx.plan.needs_db and host == ctx.plan.db_hosts[0]:
            paths.append(ctx.plan.config.remote_dir("db", ctx.sample_slug))
        if not paths:
            return
        rm_cmd = "set -euo pipefail; " + " ".join(f"rm -rf {shlex.quote(p)};" for p in paths)
        remote_exec.ssh(host, f"bash -lc {shlex.quote(rm_cmd)}", ctx.logger).check_returncode()

    failures: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        future_to_host = {ex.submit(_preclean_host, host): host for host in ctx.involved_hosts}
        for fut in concurrent.futures.as_completed(future_to_host):
            host = future_to_host[fut]
            try:
                fut.result()
            except Exception as exc:
                failures[host] = str(exc)
    if failures:
        details = "\n".join(f"- {host}: {msg}" for host, msg in sorted(failures.items()))
        raise RuntimeError(f"Pre-clean failed on remote hosts:\n{details}")


def stage_image_to_backends(ctx: DistributedBenchContext) -> None:
    def _prep_backend_dir_and_tar(host: str) -> None:
        cmd = (
            "set -euo pipefail; "
            f"mkdir -p {shlex.quote(ctx.remote_app_dirs[host])}; "
            f"if [ -f {shlex.quote(ctx.remote_tars[host])} ]; then echo HAVE_TAR; else echo NEED_TAR; fi"
        )
        out = remote_exec.ssh(host, f"bash -lc {shlex.quote(cmd)}", ctx.logger)
        out.check_returncode()
        text = (out.stdout or b"").decode(errors="ignore")
        if "NEED_TAR" in text:
            remote_exec.scp_to_remote(ctx.tar_path, host, ctx.remote_tars[host], ctx.logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.plan.backend_hosts) or 1)) as ex:
        list(ex.map(_prep_backend_dir_and_tar, list(ctx.plan.backend_hosts)))

    # Optionally delete the local tar after staging to reduce disk usage.
    # This intentionally trades re-use across runs for lower local disk pressure.
    if os.environ.get("BAXBENCH_DELETE_LOCAL_IMAGE_TAR", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            ctx.tar_path.unlink()
            ctx.logger.info("Deleted local docker image tar %s", ctx.tar_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            ctx.logger.warning("Failed to delete local docker image tar %s: %s", ctx.tar_path, exc)

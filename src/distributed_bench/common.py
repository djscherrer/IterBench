from __future__ import annotations

import concurrent.futures
import logging
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
        list(ex.map(lambda h: remote_exec.ensure_rootless_docker(h, ctx.logger), ctx.involved_hosts))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ssh_warmup(h, ctx.logger), ctx.involved_hosts))


def preclean_hosts(
    ctx: DistributedBenchContext,
    *,
    keep_backends: bool,
    keep_db: bool,
    keep_lb: bool,
) -> None:
    def _preclean_host(host: str) -> None:
        paths: list[str] = []
        if (not keep_backends) and host in ctx.remote_app_dirs:
            paths.append(ctx.remote_app_dirs[host])
        if (not keep_lb) and host == ctx.plan.lb_host:
            paths.append(ctx.plan.config.remote_dir("lb", ctx.sample_slug))
        if host == ctx.plan.load_host:
            paths.append(ctx.remote_load_dir)
        if ctx.plan.needs_db and (not keep_db) and host == ctx.plan.db_host:
            paths.append(ctx.plan.config.remote_dir("db", ctx.sample_slug))
        if not paths:
            return
        rm_cmd = "set -euo pipefail; " + " ".join(f"rm -rf {shlex.quote(p)};" for p in paths)
        remote_exec.ssh(host, f"bash -lc {shlex.quote(rm_cmd)}", ctx.logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(ctx.involved_hosts) or 1)) as ex:
        list(ex.map(_preclean_host, ctx.involved_hosts))


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

from __future__ import annotations

import concurrent.futures
import logging
import os
import shlex
from dataclasses import dataclass
from typing import Any

import remote_exec
from bench_models import RemoteConfig, host_slug
from distributed_bench.system_configs import resolve_system_topology


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    message: str


def _check_remote_dir(host: str, remote_base_dir: str, logger: logging.Logger) -> None:
    cmd = (
        "set -euo pipefail; "
        f"d={shlex.quote(remote_base_dir.rstrip('/') or '.')}; "
        "mkdir -p \"$d\"; "
        "df -h \"$d\" || true; "
        f"t=\"$d/.baxbench-write-test-{host_slug(host)}\"; "
        "touch \"$t\"; rm -f \"$t\""
    )
    remote_exec.ssh(host, f"bash -lc {shlex.quote(cmd)}", logger).check_returncode()


def _check_locust_ports(
    *,
    master_host: str,
    worker_hosts: tuple[str, ...],
    logger: logging.Logger,
    port: int = 29999,
) -> None:
    """
    Best-effort firewall/port check: start a TCP listener on master, ensure each worker can connect.
    Requires python3 on all load hosts.
    """
    pidfile = f"/tmp/baxbench-preflight-listen-{port}.pid"
    outfile = f"/tmp/baxbench-preflight-listen-{port}.out"
    readyfile = f"/tmp/baxbench-preflight-listen-{port}.ready"

    server_cmd = (
        "set -euo pipefail; "
        f"rm -f {shlex.quote(pidfile)} {shlex.quote(readyfile)}; "
        f"(python3 - <<'PY'\n"
        "import pathlib\n"
        "import socket\n"
        "import time\n"
        f"PORT = {int(port)}\n"
        f"READY = {readyfile!r}\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('0.0.0.0', PORT))\n"
        "s.listen(16)\n"
        "s.settimeout(1.0)\n"
        "pathlib.Path(READY).write_text('ready')\n"
        "end = time.time() + 20\n"
        "ok = 0\n"
        "while time.time() < end:\n"
        "    try:\n"
        "        c, _addr = s.accept()\n"
        "        c.close()\n"
        "        ok += 1\n"
        "    except Exception:\n"
        "        pass\n"
        "print(ok)\n"
        "PY\n"
        f") > {shlex.quote(outfile)} 2>&1 & "
        f"echo $! > {shlex.quote(pidfile)}"
    )
    cleanup = (
        "set -euo pipefail; "
        f"if [ -f {shlex.quote(pidfile)} ]; then kill $(cat {shlex.quote(pidfile)}) >/dev/null 2>&1 || true; fi; "
        f"rm -f {shlex.quote(pidfile)} {shlex.quote(outfile)} {shlex.quote(readyfile)}"
    )

    try:
        remote_exec.ssh(master_host, f"bash -lc {shlex.quote(server_cmd)}", logger).check_returncode()

        try:
            master_ip = remote_exec.resolve_remote_preferred_ipv4(master_host, logger, preferred_prefixes=("10.233.",))
        except Exception:
            master_ip = remote_exec.resolve_remote_primary_ipv4(master_host, logger)
        logger.info("Preflight: connectivity check master_ip=%s port=%d", master_ip, int(port))

        local_probe = (
            "set -euo pipefail; "
            f"for _ in $(seq 1 30); do [ -f {shlex.quote(readyfile)} ] && break; sleep 0.2; done; "
            f"[ -f {shlex.quote(readyfile)} ] || exit 1; "
            "python3 -c "
            + shlex.quote(
                "import socket; "
                f"HOST='127.0.0.1'; PORT={int(port)}; "
                "s=socket.socket(); s.settimeout(2.0); s.connect((HOST, PORT)); s.close();"
            )
        )
        lp = remote_exec.ssh(master_host, f"bash -lc {shlex.quote(local_probe)}", logger)
        if lp.returncode != 0:
            diag_cmd = (
                "set -euo pipefail; "
                f"echo '--- listen pid ---'; cat {shlex.quote(pidfile)} 2>/dev/null || true; "
                "echo '--- listen process ---'; "
                f"if [ -f {shlex.quote(pidfile)} ]; then ps -p $(cat {shlex.quote(pidfile)}) -o pid,cmd 2>/dev/null || true; fi; "
                "echo '--- listen sockets (ss) ---'; "
                f"(command -v ss >/dev/null 2>&1 && ss -ltnp 2>/dev/null | (grep -F ':{int(port)}' || true)) || true; "
                "echo '--- listen output ---'; "
                f"tail -n 200 {shlex.quote(outfile)} 2>/dev/null || true"
            )
            diag = remote_exec.ssh(master_host, f"bash -lc {shlex.quote(diag_cmd)}", logger)
            msg = (diag.stdout or b"").decode(errors="ignore").strip()
            raise RuntimeError(
                f"Preflight failed: master {master_host} could not start local TCP listener on 127.0.0.1:{int(port)}.\n{msg}"
            )

        def _connect(worker: str) -> None:
            ccmd = (
                "set -euo pipefail; "
                "python3 -c "
                + shlex.quote(
                    "import socket; "
                    f"HOST='{master_ip}'; PORT={int(port)}; "
                    "s=socket.socket(); s.settimeout(3.0); "
                    "s.connect((HOST, PORT)); s.close();"
                )
            )
            o = remote_exec.ssh(worker, f"bash -lc {shlex.quote(ccmd)}", logger)
            if o.returncode != 0:
                msg = (o.stdout or b"").decode(errors="ignore").strip() or f"exit {o.returncode}"
                raise RuntimeError(
                    f"Preflight failed: worker {worker} could not connect to {master_host} ({master_ip}:{int(port)}).\n{msg}"
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(worker_hosts) or 1)) as ex:
            list(ex.map(_connect, list(worker_hosts)))
    finally:
        remote_exec.ssh(master_host, f"bash -lc {shlex.quote(cleanup)}", logger)


def run_remote_preflight(config: RemoteConfig, *, logger: logging.Logger) -> PreflightResult:
    involved = sorted(
        set(
            [
                *config.backend_hosts,
                config.load_master,
                *list(config.load_workers),
                *list(config.db_hosts or ()),
                config.effective_lb_host(),
            ]
        )
        - {""}
    )
    load_hosts = sorted(set([config.load_master, *list(config.load_workers)]) - {""})

    logger.info("Preflight: involved_hosts=%s", ", ".join(involved))
    logger.info("Preflight: load_hosts=%s", ", ".join(load_hosts))
    logger.info("Preflight: remote_base_dir=%s", config.remote_base_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(involved) or 1)) as ex:
        list(ex.map(lambda h: _check_remote_dir(h, config.remote_base_dir, logger), involved))

    container_hosts = sorted(
        set([*config.backend_hosts, *list(config.db_hosts or ()), config.effective_lb_host()]) - {""}
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(container_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ensure_docker_access(h, logger), container_hosts))

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(load_hosts) or 1)) as ex:
        list(ex.map(lambda h: remote_exec.ensure_remote_python_tooling(h, logger), load_hosts))

    if len(load_hosts) > 1:
        _check_locust_ports(master_host=config.load_master, worker_hosts=tuple(config.load_workers), logger=logger)

    logger.info("Preflight OK.")
    return PreflightResult(ok=True, message="ok")


def run_preflight_from_args(args: Any) -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("baxbench.preflight")

    bench_remote_config = None
    # Require a named system topology with host mapping (no per-host CLI overrides).
    topology_name = os.environ.get("BAXBENCH_SYSTEM_TOPOLOGY", "default").strip()
    topology = resolve_system_topology(topology_name)
    if topology.has_host_mapping():
        bench_remote_config = topology.to_remote_config(
            remote_base_dir=args.bench_remote_dir,
            app_private_addr=args.bench_app_private_addr,
            app_port=args.bench_remote_port,
        )

    if bench_remote_config is None:
        raise ValueError(
            "No remote host mapping available for preflight. "
            "Set BAXBENCH_SYSTEM_TOPOLOGY to a topology with host mapping."
        )

    run_remote_preflight(bench_remote_config, logger=logger)



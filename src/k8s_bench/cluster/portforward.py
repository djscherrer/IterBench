from __future__ import annotations

import contextlib
import logging
import socket
import subprocess
import time
from collections.abc import Iterator


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def kubectl_port_forward(
    *,
    namespace: str,
    service: str,
    remote_port: int,
    local_port: int | None = None,
    ready_timeout_s: float = 15.0,
    logger: logging.Logger | None = None,
) -> Iterator[int]:
    """
    Forward ``svc/<service>`` to localhost and yield the local port.
    """
    log = logger or logging.getLogger(__name__)
    port = local_port if local_port is not None else _pick_free_port()
    proc = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "-n",
            namespace,
            f"svc/{service}",
            f"{port}:{remote_port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + ready_timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else "") or ""
            raise RuntimeError(f"kubectl port-forward exited early: {err.strip()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        proc.terminate()
        raise TimeoutError(f"port-forward to svc/{service} did not become ready on :{port}")
    log.info("kubectl port-forward svc/%s %d:%d (namespace=%s)", service, port, remote_port, namespace)
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

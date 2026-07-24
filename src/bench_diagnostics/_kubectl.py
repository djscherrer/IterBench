"""Shared ``kubectl`` subprocess helpers used by the Kubernetes collectors."""

from __future__ import annotations

import io
import os
import signal
import subprocess
from typing import Sequence


def _supports_unbuffered(stream: int | object) -> bool:
    """``bufsize=0`` requires real OS file descriptors on both streams."""
    if stream in (subprocess.DEVNULL, subprocess.PIPE):
        return True
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        return False
    try:
        fileno()
        return True
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        return False


def run(args: Sequence[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
        check=False,
    )


def spawn(
    args: Sequence[str], *, stdout: int | object, stderr: int | object
) -> subprocess.Popen[bytes]:
    popen_kwargs: dict[str, object] = {
        "stdout": stdout,
        "stderr": stderr,
        "env": os.environ.copy(),
    }
    if hasattr(os, "setsid"):
        popen_kwargs["preexec_fn"] = os.setsid
    if _supports_unbuffered(stdout) and _supports_unbuffered(stderr):
        popen_kwargs["bufsize"] = 0
    return subprocess.Popen(["kubectl", *args], **popen_kwargs)  # type: ignore[arg-type]


def terminate(proc: subprocess.Popen[bytes], *, grace_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass

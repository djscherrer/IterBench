"""Shared ``kubectl`` subprocess helpers used by the Kubernetes collectors."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Sequence


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
    return subprocess.Popen(
        ["kubectl", *args],
        stdout=stdout,
        stderr=stderr,
        env=os.environ.copy(),
        bufsize=0,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )


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

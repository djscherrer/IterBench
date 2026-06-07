from __future__ import annotations

import hashlib
import logging
import shlex

import remote_exec


class RemoteRuntime:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    @staticmethod
    def stable_port(base: int, key: str, span: int = 4000) -> int:
        hid = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
        return base + (hid % span)

    def docker_ps_id(self, host: str, *, labels: dict[str, str]) -> str:
        flt = " ".join(f"--filter label={shlex.quote(k + '=' + v)}" for k, v in labels.items())
        cmd = f"bash -lc \"docker ps -q {flt} | head -n 1\""
        out = remote_exec.ssh(host, cmd, self.logger)
        out.check_returncode()
        return (out.stdout or b"").decode(errors="ignore").strip()

    def docker_ps_name(self, host: str, *, labels: dict[str, str]) -> str:
        flt = " ".join(f"--filter label={shlex.quote(k + '=' + v)}" for k, v in labels.items())
        cmd = f"bash -lc \"docker ps --format '{{{{.Names}}}}' {flt} | head -n 1\""
        out = remote_exec.ssh(host, cmd, self.logger)
        out.check_returncode()
        return (out.stdout or b"").decode(errors="ignore").strip()

    def docker_rm_by_labels(self, host: str, *, labels: dict[str, str]) -> None:
        flt = " ".join(f"--filter label={shlex.quote(k + '=' + v)}" for k, v in labels.items())
        cmd = (
            "set -euo pipefail; "
            f"ids=$(docker ps -aq {flt} || true); "
            "if [ -n \"$ids\" ]; then docker rm -f $ids >/dev/null 2>&1 || true; fi"
        )
        remote_exec.ssh(host, f"bash -lc {shlex.quote(cmd)}", self.logger)

    def docker_image_matches(self, host: str, container_id: str, expected_image: str) -> bool:
        cmd = f"bash -lc \"docker inspect -f '{{{{.Image}}}}' {shlex.quote(container_id)} 2>/dev/null || true\""
        out = remote_exec.ssh(host, cmd, self.logger)
        txt = (out.stdout or b"").decode(errors="ignore").strip()
        return expected_image in txt

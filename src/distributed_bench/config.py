from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    txt = raw.strip()
    if not txt:
        return None
    return int(txt)


@dataclass(frozen=True)
class RuntimeToggles:
    keep_backends: bool
    keep_db: bool
    keep_lb: bool
    wipe_db_on_reuse: bool
    skip_teardown: bool
    ssh_multiplex: bool
    log_commands: bool
    collect_docker_logs: bool
    locust_processes: int | None
    system_topology: str
    load_profile: str

    @classmethod
    def from_env(cls) -> "RuntimeToggles":
        return cls(
            keep_backends=_env_bool("BAXBENCH_KEEP_BACKENDS", False),
            keep_db=_env_bool("BAXBENCH_KEEP_DB", False),
            keep_lb=_env_bool("BAXBENCH_KEEP_LB", False),
            wipe_db_on_reuse=_env_bool("BAXBENCH_WIPE_DB_ON_REUSE", True),
            skip_teardown=_env_bool("BAXBENCH_SKIP_TEARDOWN", False),
            ssh_multiplex=_env_bool("BAXBENCH_SSH_MULTIPLEX", False),
            log_commands=_env_bool("BAXBENCH_LOG_COMMANDS", True),
            collect_docker_logs=_env_bool("BAXBENCH_COLLECT_DOCKER_LOGS", True),
            locust_processes=_env_int("BAXBENCH_LOCUST_PROCESSES"),
            system_topology=os.environ.get("BAXBENCH_SYSTEM_TOPOLOGY", "default").strip(),
            load_profile=os.environ.get("BAXBENCH_LOAD_PROFILE", "default").strip(),
        )

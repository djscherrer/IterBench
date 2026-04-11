from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LoadProfile:
    name: str
    users: int
    spawn_rate: int
    run_time_s: int
    wait_min_s: float = 0.5
    wait_max_s: float = 1.5
    locust_processes: int = 1
    extra_locust_args: tuple[str, ...] = field(default_factory=tuple)

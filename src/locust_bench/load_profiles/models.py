from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class BaseLoadProfile:
    name: str
    wait_min_s: float
    wait_max_s: float
    # Number of OS processes Locust should use on each load-generator host.
    locust_processes: int

    @property
    def effective_users(self) -> int:
        raise NotImplementedError

    @property
    def effective_spawn_rate(self) -> int:
        raise NotImplementedError

    @property
    def effective_run_time_s(self) -> int:
        raise NotImplementedError


@dataclass(frozen=True)
class SteadyLoadProfile(BaseLoadProfile):
    # Hold a constant number of users for the whole run.
    users: int
    # Total runtime in seconds.
    run_time_s: int

    @property
    def effective_users(self) -> int:
        return int(self.users)

    @property
    def effective_spawn_rate(self) -> int:
        return max(1, int(self.users))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


@dataclass(frozen=True)
class ContinuousLoadProfile(BaseLoadProfile):
    # Linearly ramp from start_users -> target_users over the runtime.
    start_users: int
    target_users: int
    spawn_rate: int
    # Total runtime in seconds.
    run_time_s: int

    @property
    def effective_users(self) -> int:
        return int(self.target_users)

    @property
    def effective_spawn_rate(self) -> int:
        return int(self.spawn_rate)

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


@dataclass(frozen=True)
class StairsLoadProfile(BaseLoadProfile):
    # Start at start_users; every step_duration_s increase by step_users (for `steps` steps).
    start_users: int
    step_users: int
    step_duration_s: int
    steps: int
    # Total runtime in seconds. Convention: if this is 0, treat runtime as
    # step_duration_s * steps.
    run_time_s: int

    @property
    def effective_users(self) -> int:
        return int(self.start_users + (self.step_users * self.steps))

    @property
    def effective_spawn_rate(self) -> int:
        return max(1, int(self.step_users))

    @property
    def effective_run_time_s(self) -> int:
        if self.run_time_s <= 0:
            return int(self.step_duration_s * self.steps)
        return int(self.run_time_s)


@dataclass(frozen=True)
class SpikeLoadProfile(BaseLoadProfile):
    # Base load most of the time, with periodic spikes.
    base_users: int
    spike_users: int
    # Repeating window of ``interval_s`` seconds: hold ``spike_users`` for the first
    # ``duration_s`` seconds, then ``base_users`` for the remainder (e.g. interval 30,
    # duration 10 → 10s spike, 20s at base).
    interval_s: int
    duration_s: int
    # Total runtime in seconds.
    run_time_s: int

    @property
    def effective_users(self) -> int:
        return int(max(self.base_users, self.spike_users))

    @property
    def effective_spawn_rate(self) -> int:
        return max(1, abs(int(self.spike_users) - int(self.base_users)))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


@dataclass(frozen=True)
class AdaptiveLoadProfile(BaseLoadProfile):
    """
    Adaptive load profile: Locust shape adjusts users based on live latency.

    The actual control loop runs inside `_baxbench_shape.AdaptiveShape` using Locust Environment stats.
    This profile only provides static configuration + a maximum runtime.
    """

    sla_ms: float
    start_users: int
    max_users: int
    min_step_users: int
    max_step_users: int
    step_duration_s: int
    trim_s: int
    sample_every_s: int
    settle_samples: int
    quantile: float
    run_time_s: int

    @property
    def effective_users(self) -> int:
        # Planner still needs a final \"users\" number; use max_users as the ceiling.
        return int(self.max_users)

    @property
    def effective_spawn_rate(self) -> int:
        # Use max step as a reasonable upper bound for spawn changes.
        return max(1, int(self.max_step_users))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


LoadProfile: TypeAlias = (
    SteadyLoadProfile | ContinuousLoadProfile | StairsLoadProfile | SpikeLoadProfile | AdaptiveLoadProfile
)

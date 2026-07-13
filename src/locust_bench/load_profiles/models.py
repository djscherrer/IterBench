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
    spawn_rate: int
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
        return max(1, int(self.spawn_rate))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


@dataclass(frozen=True)
class AdaptiveV2LoadProfile(BaseLoadProfile):
    """
    Adaptive load profile v2.

    Extends the v1 control loop with the changes we discussed:

    1. **Explicit warm-up phase.** The first ``warmup_step_duration_s`` seconds at
       ``start_users`` are not used for any decision: JIT warm-up, cold caches,
       and DB connection-pool spin-up cannot poison the first SLA check.
    2. **Failure rate is an SLA signal.** Any step where the *step-local*
       failure rate exceeds ``failure_threshold_pct`` is treated as an SLA
       violation, regardless of p95 latency — a system that returns errors fast
       is not "below SLA".
    3. **Banded ramp-up.** Instead of a single growth rule, the next step is
       multiplied by 4x / 2x / 1x / 0.5x depending on how far below SLA p95 sits
       (>=70% / 40% / 15% / <15% margin). Big headroom is exploited aggressively,
       tight headroom is approached carefully.
    4. **Stability heuristic.** A step is only "ended" once the first→last drift
       across the last ``min_settle_samples`` windowed p95 readings is at most
       ``stability_drift_threshold_pct`` (default 5%), with a hard
       ``max_step_duration_s`` cap. The decision latency is the mean of those
       samples. This avoids deciding while the trailing P95 is still trending.
    5. **Goodput-plateau stop.** Tracks successful RPS per step. After
       ``plateau_stop_steps`` consecutive steps with relative goodput growth
       below ``plateau_goodput_threshold_pct``, the shape stops — this is the
       saturation point we actually care about.
    6. **Final reporting.** When the shape terminates, it emits a single
       ``adaptive-v2 stop`` log line with the reason (plateau / bracket / max
       users / SLA-floor / run-time), final users, low/high bracket, and the
       per-step goodput history so the experiment summary can render it.
    """

    sla_ms: float
    failure_threshold_pct: float
    start_users: int
    max_users: int
    min_step_users: int
    max_step_users: int
    spawn_rate: int

    warmup_step_duration_s: int
    min_step_duration_s: int
    max_step_duration_s: int
    trim_s: int
    sample_every_s: int
    min_settle_samples: int
    quantile: float
    stability_drift_threshold_pct: float

    plateau_stop_steps: int
    plateau_goodput_threshold_pct: float

    run_time_s: int

    @property
    def effective_users(self) -> int:
        return int(self.max_users)

    @property
    def effective_spawn_rate(self) -> int:
        return max(1, int(self.spawn_rate))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


@dataclass(frozen=True)
class GoodputPlateauLoadProfile(BaseLoadProfile):
    """
    Goodput plateau finder.

    Control loop runs in ``_baxbench_shape.GoodputPlateauShape``: ramp with an
    initial ``max_step_users`` jump, then efficiency-based steps (``step_up_gain``
    when marginal efficiency is high, eff³ when low). Backs off up to
    ``overload_backoff_max`` times on overload, then stops. Also stops after
    ``plateau_stop_steps`` consecutive passing steps (stable or unstable-cap)
    that do not beat the best stable goodput seen so far.
    """

    failure_threshold_pct: float
    collapse_threshold_pct: float
    overload_p95_ms: float
    start_users: int
    max_users: int
    min_step_users: int
    max_step_users: int
    step_up_gain: float
    efficiency_good_threshold: float
    drain_time_s: int
    spawn_rate: int

    warmup_step_duration_s: int
    min_step_duration_s: int
    max_step_duration_s: int
    trim_s: int
    sample_every_s: int
    min_settle_samples: int
    quantile: float
    stability_drift_threshold_pct: float

    plateau_stop_steps: int
    plateau_goodput_threshold_pct: float
    overload_backoff_max: int

    run_time_s: int

    @property
    def effective_users(self) -> int:
        return int(self.max_users)

    @property
    def effective_spawn_rate(self) -> int:
        return max(1, int(self.spawn_rate))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


@dataclass(frozen=True)
class ExploreRefineLoadProfile(BaseLoadProfile):
    """
    Explore then refine goodput finder.

    Health thresholds (``failure_threshold_pct``, ``overload_p95_ms``, etc.) apply
    across all phases. Phase-specific tuning uses ``explore_*``, ``recovery_*``,
    and ``refine_*`` prefixes.
    """

    # Health / SLA (all phases)
    failure_threshold_pct: float
    collapse_threshold_pct: float
    overload_p95_ms: float

    # Run limits
    start_users: int
    max_users: int
    spawn_rate: int
    run_time_s: int

    # Shared sampling
    sample_every_s: int
    quantile: float
    stability_drift_threshold_pct: float

    # Explore
    explore_warmup_duration_s: int
    explore_ramp_user_fraction_per_s: float
    explore_min_step_users: int
    explore_goodput_stop_ratio: float
    explore_stop_steps: int

    # Recovery (fraction floor + settle, latency-gated exit)
    recovery_floor_fraction: float
    recovery_settle_duration_s: int
    recovery_retry_drop_fraction: float

    # Refine (fixed-level observation and stepping)
    refine_min_step_duration_s: int
    refine_max_step_duration_s: int
    refine_min_settle_samples: int
    refine_measure_window_s: int
    refine_min_step_users: int
    refine_max_step_users: int
    refine_initial_step_fraction: float
    refine_max_step_fraction: float
    refine_efficiency_good_threshold: float
    refine_step_growth: float
    refine_stop_steps: int
    refine_overload_backoff_max: int

    # Shape runtime (written to manifest; no implicit defaults in the shape)
    health_grace_s: int
    spawn_target_duration_s: float
    spawn_settle_buffer_s: float
    abort_on_no_users: bool

    @property
    def effective_users(self) -> int:
        return int(self.max_users)

    @property
    def effective_spawn_rate(self) -> int:
        return max(1, int(self.spawn_rate))

    @property
    def effective_run_time_s(self) -> int:
        return int(self.run_time_s)


LoadProfile: TypeAlias = (
    SteadyLoadProfile
    | ContinuousLoadProfile
    | StairsLoadProfile
    | SpikeLoadProfile
    | AdaptiveLoadProfile
    | AdaptiveV2LoadProfile
    | GoodputPlateauLoadProfile
    | ExploreRefineLoadProfile
)

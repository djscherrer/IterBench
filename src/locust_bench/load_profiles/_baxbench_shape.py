from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass

from locust import LoadTestShape, between

_LOG = logging.getLogger("baxbench.adaptive")


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return int(default)
    try:
        return int(float(v))
    except Exception:
        return int(default)


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name, "").strip()
    if not v:
        return float(default)
    try:
        return float(v)
    except Exception:
        return float(default)


def baxbench_wait_time():
    """
    Locust wait_time callable configured via:
      - BAXBENCH_LOCUST_WAIT_MIN_S
      - BAXBENCH_LOCUST_WAIT_MAX_S
    """
    wmin = _get_float("BAXBENCH_LOCUST_WAIT_MIN_S", 0.5)
    wmax = _get_float("BAXBENCH_LOCUST_WAIT_MAX_S", 1.5)
    # Guard against swapped inputs
    lo, hi = (wmin, wmax) if wmin <= wmax else (wmax, wmin)
    return between(lo, hi)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class _AdaptiveParams:
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
    health_grace_s: int
    abort_on_no_users: bool


def _adaptive_params_from_env() -> _AdaptiveParams:
    trim_s = max(0, _get_int("BAXBENCH_ADAPTIVE_TRIM_S", 20))
    return _AdaptiveParams(
        sla_ms=float(_get_float("BAXBENCH_ADAPTIVE_SLA_MS", 300.0)),
        start_users=max(0, _get_int("BAXBENCH_ADAPTIVE_START_USERS", 500)),
        max_users=max(1, _get_int("BAXBENCH_ADAPTIVE_MAX_USERS", 20_000)),
        min_step_users=max(1, _get_int("BAXBENCH_ADAPTIVE_MIN_STEP_USERS", 50)),
        max_step_users=max(1, _get_int("BAXBENCH_ADAPTIVE_MAX_STEP_USERS", 400)),
        spawn_rate=max(1, _get_int("BAXBENCH_ADAPTIVE_SPAWN_RATE", 50)),
        step_duration_s=max(5, _get_int("BAXBENCH_ADAPTIVE_STEP_DURATION_S", 45)),
        trim_s=trim_s,
        sample_every_s=max(1, _get_int("BAXBENCH_ADAPTIVE_SAMPLE_EVERY_S", 5)),
        settle_samples=max(1, _get_int("BAXBENCH_ADAPTIVE_SETTLE_SAMPLES", 3)),
        quantile=float(_get_float("BAXBENCH_ADAPTIVE_QUANTILE", 0.95)),
        health_grace_s=max(5, _get_int("BAXBENCH_ADAPTIVE_HEALTH_GRACE_S", max(15, trim_s))),
        abort_on_no_users=_env_bool("BAXBENCH_ADAPTIVE_ABORT_NO_USERS", True),
    )


class _BaseShape(LoadTestShape):
    def _should_stop(self) -> bool:
        run_time_s = max(1, _get_int("BAXBENCH_RUN_TIME_S", 1))
        return float(self.get_run_time()) >= float(run_time_s)


class SteadyShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        steady_users = max(0, _get_int("BAXBENCH_STEADY_USERS", 0))
        return steady_users, max(1, steady_users)


class ContinuousShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        run_time_s = max(1, _get_int("BAXBENCH_RUN_TIME_S", 1))
        spawn_rate = max(1, _get_int("BAXBENCH_CONTINUOUS_SPAWN_RATE", 1))
        start = max(0, _get_int("BAXBENCH_CONTINUOUS_START_USERS", 0))
        target = max(start, _get_int("BAXBENCH_CONTINUOUS_TARGET_USERS", start))
        t = float(self.get_run_time())
        if run_time_s <= 1:
            return target, spawn_rate
        frac = min(1.0, max(0.0, t / float(run_time_s)))
        users = int(round(start + (target - start) * frac))
        return max(0, users), spawn_rate


class StairsShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        start = max(0, _get_int("BAXBENCH_STAIRS_START_USERS", 0))
        step_users = max(0, _get_int("BAXBENCH_STAIRS_STEP_USERS", 100))
        step_dur = max(1, _get_int("BAXBENCH_STAIRS_STEP_DURATION_S", 30))
        steps = max(1, _get_int("BAXBENCH_STAIRS_STEPS", 10))
        t = float(self.get_run_time())
        idx = int(t // float(step_dur))
        idx = min(idx, steps)  # allow last step to persist until stop
        users = start + (step_users * idx)
        return max(0, users), max(1, step_users)


class SpikeShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        base = max(0, _get_int("BAXBENCH_SPIKE_BASE_USERS", 500))
        spike = max(base, _get_int("BAXBENCH_SPIKE_USERS", 1000))
        interval = max(1, _get_int("BAXBENCH_SPIKE_INTERVAL_S", 30))
        dur = max(1, _get_int("BAXBENCH_SPIKE_DURATION_S", 10))
        t = float(self.get_run_time())
        in_spike = (t % float(interval)) < float(dur)
        users = spike if in_spike else base
        return max(0, users), max(1, abs(spike - base))


class AdaptiveShape(_BaseShape):
    """
    Adaptive load controller that adjusts users based on live latency from Locust Environment.

    Uses `self.environment.stats.total.get_response_time_percentile(q)` for feedback.
    """

    def __init__(self):
        super().__init__()
        self._p = _adaptive_params_from_env()
        self._level_start_t = 0.0
        self._next_sample_t = 0.0
        self._p95_samples: list[float] = []

        self._users = int(self._p.start_users)
        self._step = int(self._p.max_step_users)
        self._low_ok: int | None = None
        self._high_bad: int | None = None
        self._done = False
        self._abort_logged = False
        _LOG.info(
            "adaptive start: users=%s spawn_rate=%s sla_ms=%s step_duration_s=%s trim_s=%s "
            "quantile=%s health_grace_s=%s abort_on_no_users=%s",
            self._users,
            self._p.spawn_rate,
            self._p.sla_ms,
            self._p.step_duration_s,
            self._p.trim_s,
            self._p.quantile,
            self._p.health_grace_s,
            self._p.abort_on_no_users,
        )

    def _locust_environment(self):
        env = getattr(self, "environment", None)
        if env is not None:
            return env
        runner = getattr(self, "runner", None)
        return getattr(runner, "environment", None) if runner is not None else None

    def _active_user_count(self) -> int | None:
        runner = getattr(self, "runner", None)
        if runner is None:
            return None
        try:
            return int(self.get_current_user_count())
        except Exception:
            return None

    def _should_abort_no_healthy_users(self, t: float) -> bool:
        """
        Stop the shape early when all Locust users have died (e.g. bootstrap hard-fail).

        Waits ``health_grace_s`` after each level starts so spawn is not mistaken for failure.
        """
        if not self._p.abort_on_no_users:
            return False
        if int(self._users) <= 0:
            return False
        if (t - self._level_start_t) < float(self._p.health_grace_s):
            return False

        active = self._active_user_count()
        if active is None:
            return False
        if active > 0:
            return False

        if not self._abort_logged:
            self._abort_logged = True
            _LOG.error(
                "adaptive abort at t=%.0fs: zero active users (target=%s, grace=%ss); %s",
                t,
                self._users,
                self._p.health_grace_s,
                self._stats_snapshot(),
            )
        self._done = True
        return True

    def _stats_snapshot(self) -> str:
        env = self._locust_environment()
        if env is None:
            return "stats=n/a"
        total = getattr(getattr(env, "stats", None), "total", None)
        if total is None:
            return "stats=n/a"
        reqs = int(getattr(total, "num_requests", 0) or 0)
        fails = int(getattr(total, "num_failures", 0) or 0)
        fail_pct = f"{100.0 * fails / reqs:.1f}%" if reqs else "n/a"
        lat = self._read_latency_ms()
        lat_s = f"{lat:.0f}ms" if lat is not None else "n/a"
        return f"reqs={reqs} fail={fails} ({fail_pct}) p{int(self._p.quantile * 100)}={lat_s}"

    def _read_latency_ms(self) -> float | None:
        env = self._locust_environment()
        if env is None:
            return None
        stats = getattr(env, "stats", None)
        if stats is None:
            return None
        total = getattr(stats, "total", None)
        if total is None:
            return None
        try:
            v = total.get_response_time_percentile(float(self._p.quantile))
        except Exception:
            return None
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _enter_level(self, t: float) -> None:
        self._level_start_t = t
        self._next_sample_t = t
        self._p95_samples = []
        self._abort_logged = False

    def _decide_and_advance(self, summary_p95_ms: float) -> str:
        sla = float(self._p.sla_ms)
        below = summary_p95_ms <= sla
        prev_users = int(self._users)
        action = ""

        if below:
            self._low_ok = int(self._users)
        else:
            self._high_bad = int(self._users)

        # If we have a bracket, do a binary-search style refinement.
        if self._low_ok is not None and self._high_bad is not None and self._high_bad > self._low_ok:
            gap = int(self._high_bad - self._low_ok)
            if gap <= int(self._p.min_step_users):
                self._done = True
                action = f"bracket narrow (low={self._low_ok} high={self._high_bad}); stopping shape"
                return action
            self._step = max(int(self._p.min_step_users), min(int(self._p.max_step_users), int(gap // 2)))
            self._users = min(int(self._p.max_users), int(self._low_ok + self._step))
            action = f"bracket refine low={self._low_ok} high={self._high_bad} -> users={self._users} step={self._step}"
            return action

        # No bracket yet: explore.
        if below:
            margin = max(0.0, sla - summary_p95_ms)
            if margin >= 0.5 * sla:
                self._step = min(int(self._p.max_step_users), int(self._step * 2))
            elif margin <= 0.15 * sla:
                self._step = max(int(self._p.min_step_users), int(self._step // 2))
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            action = f"below SLA (p95={summary_p95_ms:.0f}ms) ramp +{self._users - prev_users} -> users={self._users}"
        else:
            self._step = max(int(self._p.min_step_users), int(self._step // 2))
            self._users = max(0, int(self._users - self._step))
            action = f"above SLA (p95={summary_p95_ms:.0f}ms) backoff -> users={self._users} step={self._step}"
        return action

    def _spawn_rate(self) -> int:
        """Cap spawn rate so we do not spawn hundreds of users in sub-second bursts."""
        return max(1, min(int(self._p.spawn_rate), int(self._step), int(self._users)))

    def tick(self):
        if self._should_stop() or self._done:
            return None

        t = float(self.get_run_time())
        if self._level_start_t <= 0.0:
            self._enter_level(t)

        if self._should_abort_no_healthy_users(t):
            return None

        # Sample latency periodically.
        if t >= self._next_sample_t:
            # Only collect samples after trim to avoid ramp/cache noise dominating decisions.
            if (t - self._level_start_t) >= float(self._p.trim_s):
                v = self._read_latency_ms()
                if v is not None and v > 0:
                    self._p95_samples.append(v)
            self._next_sample_t = t + float(self._p.sample_every_s)

        elapsed = t - self._level_start_t
        if elapsed >= float(self._p.step_duration_s):
            samples = self._p95_samples
            if len(samples) >= int(self._p.settle_samples):
                summary = statistics.median(samples[-int(self._p.settle_samples) :])
                action = self._decide_and_advance(float(summary))
                _LOG.info(
                    "adaptive phase end t=%.0fs: %s | %s | %s",
                    t,
                    action,
                    self._stats_snapshot(),
                    f"samples={samples[-int(self._p.settle_samples) :]}",
                )
            else:
                _LOG.warning(
                    "adaptive phase end t=%.0fs: insufficient latency samples (%d/%d after trim); "
                    "users stay %s | %s",
                    t,
                    len(samples),
                    int(self._p.settle_samples),
                    self._users,
                    self._stats_snapshot(),
                )
            self._enter_level(t)

        return int(self._users), self._spawn_rate()


def _selected_shape_class() -> type[_BaseShape]:
    mode = (os.getenv("BAXBENCH_LOAD_MODE", "steady") or "steady").strip().lower()
    mapping: dict[str, type[_BaseShape]] = {
        "steady": SteadyShape,
        "continuous": ContinuousShape,
        "stairs": StairsShape,
        "spike": SpikeShape,
        "adaptive": AdaptiveShape,
    }
    return mapping.get(mode, SteadyShape)


# Backwards-compatible export used by scenario locustfiles:
# `from _baxbench_shape import BaxbenchShape`
BaxbenchShape = _selected_shape_class()


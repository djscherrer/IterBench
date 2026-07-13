from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from locust import LoadTestShape, between

_LOAD_PROFILE_MANIFEST = "baxbench_load_profile.json"

_LOG = logging.getLogger("baxbench.adaptive")

_manifest: dict | None = None

_EXPLORE_REFINE_REQUIRED_KEYS: tuple[str, ...] = (
    "failure_threshold_pct",
    "collapse_threshold_pct",
    "overload_p95_ms",
    "start_users",
    "max_users",
    "spawn_rate",
    "run_time_s",
    "sample_every_s",
    "quantile",
    "stability_drift_threshold_pct",
    "explore_warmup_duration_s",
    "explore_ramp_user_fraction_per_s",
    "explore_min_step_users",
    "explore_goodput_stop_ratio",
    "explore_stop_steps",
    "recovery_floor_fraction",
    "recovery_settle_duration_s",
    "recovery_retry_drop_fraction",
    "refine_min_step_duration_s",
    "refine_max_step_duration_s",
    "refine_min_settle_samples",
    "refine_measure_window_s",
    "refine_min_step_users",
    "refine_max_step_users",
    "refine_initial_step_fraction",
    "refine_max_step_fraction",
    "refine_efficiency_good_threshold",
    "refine_step_growth",
    "refine_stop_steps",
    "refine_overload_backoff_max",
    "health_grace_s",
    "spawn_target_duration_s",
    "spawn_settle_buffer_s",
    "abort_on_no_users",
)


def _manifest_required(cfg: dict, key: str):
    if key not in cfg:
        mode = cfg.get("mode", "?")
        raise KeyError(
            f"load profile manifest missing required key {key!r} (mode={mode!r})"
        )
    return cfg[key]


def _validate_explore_refine_manifest(cfg: dict) -> None:
    missing = [k for k in _EXPLORE_REFINE_REQUIRED_KEYS if k not in cfg]
    if missing:
        raise KeyError(
            "explore_refine manifest missing required keys: "
            + ", ".join(sorted(missing))
        )


def _load_manifest() -> dict:
    global _manifest
    if _manifest is not None:
        return _manifest
    candidates = [
        Path(__file__).resolve().parent / _LOAD_PROFILE_MANIFEST,
        Path.cwd() / _LOAD_PROFILE_MANIFEST,
        Path.cwd() / "locust" / _LOAD_PROFILE_MANIFEST,
    ]
    for path in candidates:
        if path.is_file():
            _manifest = json.loads(path.read_text(encoding="utf-8"))
            return _manifest
    raise FileNotFoundError(
        f"Missing {_LOAD_PROFILE_MANIFEST} beside the locustfile. "
        "Stage it with prepare_locust_run_dir()."
    )


def baxbench_wait_time():
    """Locust wait_time callable configured via the load profile manifest."""
    cfg = _load_manifest()
    wmin = float(cfg["wait_min_s"])
    wmax = float(cfg["wait_max_s"])
    lo, hi = (wmin, wmax) if wmin <= wmax else (wmax, wmin)
    return between(lo, hi)


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


def _adaptive_params_from_manifest() -> _AdaptiveParams:
    cfg = _load_manifest()
    trim_s = max(0, int(cfg["trim_s"]))
    return _AdaptiveParams(
        sla_ms=float(cfg["sla_ms"]),
        start_users=max(0, int(cfg["start_users"])),
        max_users=max(1, int(cfg["max_users"])),
        min_step_users=max(1, int(cfg["min_step_users"])),
        max_step_users=max(1, int(cfg["max_step_users"])),
        spawn_rate=max(1, int(cfg["spawn_rate"])),
        step_duration_s=max(5, int(cfg["step_duration_s"])),
        trim_s=trim_s,
        sample_every_s=max(1, int(cfg["sample_every_s"])),
        settle_samples=max(1, int(cfg["settle_samples"])),
        quantile=float(cfg["quantile"]),
        health_grace_s=max(5, int(cfg["health_grace_s"])),
        abort_on_no_users=bool(cfg["abort_on_no_users"]),
    )


class _BaseShape(LoadTestShape):
    def _should_stop(self) -> bool:
        run_time_s = max(1, int(_load_manifest()["run_time_s"]))
        return float(self.get_run_time()) >= float(run_time_s)


class SteadyShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        cfg = _load_manifest()
        steady_users = max(0, int(cfg.get("steady_users", cfg["users"])))
        return steady_users, max(1, steady_users)


class ContinuousShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        cfg = _load_manifest()
        run_time_s = max(1, int(cfg["run_time_s"]))
        spawn_rate = max(1, int(cfg["spawn_rate"]))
        start = max(0, int(cfg["start_users"]))
        target = max(start, int(cfg["target_users"]))
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
        cfg = _load_manifest()
        start = max(0, int(cfg["start_users"]))
        step_users = max(0, int(cfg["step_users"]))
        step_dur = max(1, int(cfg["step_duration_s"]))
        steps = max(1, int(cfg["steps"]))
        t = float(self.get_run_time())
        idx = int(t // float(step_dur))
        idx = min(idx, steps)
        users = start + (step_users * idx)
        return max(0, users), max(1, step_users)


class SpikeShape(_BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        cfg = _load_manifest()
        base = max(0, int(cfg["base_users"]))
        spike = max(base, int(cfg["spike_users"]))
        interval = max(1, int(cfg["interval_s"]))
        dur = max(1, int(cfg["duration_s"]))
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
        self._p = _adaptive_params_from_manifest()
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


@dataclass(frozen=True)
class _AdaptiveV2Params:
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

    health_grace_s: int
    abort_on_no_users: bool


def _adaptive_v2_params_from_manifest() -> _AdaptiveV2Params:
    cfg = _load_manifest()
    trim_s = max(0, int(cfg["trim_s"]))
    min_step_s = max(5, int(cfg["min_step_duration_s"]))
    max_step_s = max(min_step_s, int(cfg["max_step_duration_s"]))
    return _AdaptiveV2Params(
        sla_ms=float(cfg["sla_ms"]),
        failure_threshold_pct=float(cfg["failure_threshold_pct"]),
        start_users=max(1, int(cfg["start_users"])),
        max_users=max(1, int(cfg["max_users"])),
        min_step_users=max(1, int(cfg["min_step_users"])),
        max_step_users=max(1, int(cfg["max_step_users"])),
        spawn_rate=max(1, int(cfg["spawn_rate"])),
        warmup_step_duration_s=max(0, int(cfg["warmup_step_duration_s"])),
        min_step_duration_s=min_step_s,
        max_step_duration_s=max_step_s,
        trim_s=trim_s,
        sample_every_s=max(1, int(cfg["sample_every_s"])),
        min_settle_samples=max(2, int(cfg["min_settle_samples"])),
        quantile=float(cfg["quantile"]),
        stability_drift_threshold_pct=float(cfg["stability_drift_threshold_pct"]),
        plateau_stop_steps=max(2, int(cfg["plateau_stop_steps"])),
        plateau_goodput_threshold_pct=float(cfg["plateau_goodput_threshold_pct"]),
        health_grace_s=max(5, int(cfg["health_grace_s"])),
        abort_on_no_users=bool(cfg["abort_on_no_users"]),
    )


def _latency_drift_pct(samples: list[float]) -> float:
    """Relative change from first to last sample, as a percentage (0–100+)."""
    if len(samples) < 2:
        return 0.0
    first = float(samples[0])
    last = float(samples[-1])
    if first <= 0:
        return 0.0 if last <= 0 else 100.0
    return abs(last - first) / first * 100.0


def _sample_spread_pct(samples: list[float]) -> float:
    """Peak-to-trough spread relative to the mean, as a percentage (0–100+)."""
    if len(samples) < 2:
        return 0.0
    lo = float(min(samples))
    hi = float(max(samples))
    mean = float(statistics.mean(samples))
    if mean <= 0:
        return 100.0 if hi > 0 else 0.0
    # Percent spread is a good stability proxy at moderate/large latencies, but
    # it becomes misleading when mean p95 is tiny: e.g. 7ms→9ms is only 2ms
    # absolute movement yet ~25% spread. Use a denominator floor so low-latency
    # regimes are judged by (mostly) absolute movement.
    denom = max(mean, 50.0)
    return (hi - lo) / denom * 100.0


class AdaptiveV2Shape(_BaseShape):
    """
    Smarter adaptive controller.

    See ``AdaptiveV2LoadProfile`` for the rationale. The control loop is:

    - Step 0 is a warm-up at ``start_users``; nothing it observes ever feeds a
      decision.
    - Each subsequent step samples Locust's trailing ~10s p95 every
      ``sample_every_s`` after a ``trim_s`` cool-in (keep ``trim_s`` >= the 10s
      window so a sample never straddles the previous level). Once
      ``min_settle_samples`` samples are stable (first→last drift ≤
      ``stability_drift_threshold_pct``), or ``max_step_duration_s`` is reached,
      the step ends and a decision is made.
    - **SLA = p95 ≤ ``sla_ms`` AND step failure rate ≤
      ``failure_threshold_pct``.** Either failure trips backoff.
    - Below SLA: ramp by adding a fraction of the *current* user count equal to
      roughly the remaining p95 headroom (+100% when slack >= 70%, then +50% /
      +40% / +30% / +20% / +10% / +5% / +2% as p95 nears the SLA; see
      ``_ramp_factor``), clamped to ``[min_step_users, max_step_users]``. The
      step self-scales, so it shrinks near the edge instead of overshooting.
    - Above SLA: halve the step and back off.
    - A bracket forms as soon as both a passing and a failing users level exist;
      when ``high_bad - low_ok ≤ min_step_users`` the shape stops.
    - The shape also stops once ``plateau_stop_steps`` consecutive *passing*
      steps grew goodput by less than ``plateau_goodput_threshold_pct``.
    - Final reason is logged as ``adaptive-v2 stop: reason=...``.
    """

    def __init__(self):
        super().__init__()
        self._p = _adaptive_v2_params_from_manifest()
        self._users = int(self._p.start_users)
        self._step = int(self._p.max_step_users)
        self._level_start_t = 0.0
        self._next_sample_t = 0.0
        self._lat_samples: list[float] = []
        self._level_start_reqs = 0
        self._level_start_fails = 0
        self._is_warmup = self._p.warmup_step_duration_s > 0
        self._goodput_history: list[tuple[int, float]] = []  # (users, goodput rps)
        self._low_ok: int | None = None
        self._high_bad: int | None = None
        self._done = False
        self._stop_reason: str | None = None
        self._final_logged = False
        self._abort_logged = False
        self._window_unavailable_logged = False
        _LOG.info(
            "adaptive-v2 start: users=%s spawn_rate=%s sla_ms=%s failure_thr_pct=%s "
            "warmup_s=%s step_duration_s=[%s..%s] trim_s=%s quantile=%s "
            "stability_drift_pct=%s plateau_stop_steps=%s plateau_pct=%s max_users=%s",
            self._users,
            self._p.spawn_rate,
            self._p.sla_ms,
            self._p.failure_threshold_pct,
            self._p.warmup_step_duration_s,
            self._p.min_step_duration_s,
            self._p.max_step_duration_s,
            self._p.trim_s,
            self._p.quantile,
            self._p.stability_drift_threshold_pct,
            self._p.plateau_stop_steps,
            self._p.plateau_goodput_threshold_pct,
            self._p.max_users,
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

    def _totals(self) -> tuple[int, int]:
        env = self._locust_environment()
        total = getattr(getattr(env, "stats", None), "total", None) if env else None
        if total is None:
            return 0, 0
        reqs = int(getattr(total, "num_requests", 0) or 0)
        fails = int(getattr(total, "num_failures", 0) or 0)
        return reqs, fails

    def _read_latency_ms(self) -> float | None:
        """p95 over Locust's trailing ~10s window, not the whole run.

        ``get_current_response_time_percentile`` reads only the last
        ``CURRENT_RESPONSE_TIME_PERCENTILE_WINDOW`` seconds (10s in Locust) and
        does NOT mutate the cumulative stats, so each step is measured cleanly
        and the post-overshoot/backed-off level is not dragged up by history.
        We deliberately avoid ``stats.reset_all()`` because that would zero the
        cumulative counters mid-run and corrupt ``default_stats*.csv``. Pair this
        with ``trim_s >= 10`` so the window never straddles the previous level.
        Falls back to the cumulative percentile only if the windowed value is
        unavailable (e.g. cache not yet populated).
        """
        env = self._locust_environment()
        total = getattr(getattr(env, "stats", None), "total", None) if env else None
        if total is None:
            return None
        v = None
        try:
            v = total.get_current_response_time_percentile(float(self._p.quantile))
        except Exception:
            v = None
        if v is None:
            if not self._window_unavailable_logged:
                self._window_unavailable_logged = True
                _LOG.warning(
                    "adaptive-v2: windowed p95 unavailable; falling back to "
                    "cumulative percentile (per-step latency may be polluted by history)"
                )
            try:
                v = total.get_response_time_percentile(float(self._p.quantile))
            except Exception:
                return None
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    def _stats_snapshot(self) -> str:
        reqs, fails = self._totals()
        fail_pct = f"{100.0 * fails / reqs:.1f}%" if reqs else "n/a"
        lat = self._read_latency_ms()
        lat_s = f"{lat:.0f}ms" if lat is not None else "n/a"
        return (
            f"reqs={reqs} fail={fails} ({fail_pct}) "
            f"p{int(self._p.quantile * 100)}={lat_s}"
        )

    def _should_abort_no_healthy_users(self, t: float) -> bool:
        if not self._p.abort_on_no_users:
            return False
        if int(self._users) <= 0:
            return False
        if (t - self._level_start_t) < float(self._p.health_grace_s):
            return False
        active = self._active_user_count()
        if active is None or active > 0:
            return False
        if not self._abort_logged:
            self._abort_logged = True
            _LOG.error(
                "adaptive-v2 abort at t=%.0fs: zero active users (target=%s, grace=%ss); %s",
                t,
                self._users,
                self._p.health_grace_s,
                self._stats_snapshot(),
            )
        self._stop_reason = "no-healthy-users"
        self._done = True
        return True

    def _enter_level(self, t: float) -> None:
        self._level_start_t = t
        self._next_sample_t = t
        self._lat_samples = []
        reqs, fails = self._totals()
        self._level_start_reqs = reqs
        self._level_start_fails = fails
        self._abort_logged = False

    @staticmethod
    def _ramp_factor(margin_frac: float) -> tuple[float, str]:
        """Fraction of the *current* user count to add for the next step.

        ``margin_frac = (sla - p95) / sla`` is the p95 headroom below the SLA
        (larger == more headroom). The step grows the user count by roughly the
        fraction of headroom we still have, so it self-scales and shrinks as p95
        approaches the SLA — we crawl up to the edge instead of overshooting.
        Only when there is a lot of slack (>= 70%) do we double. The percentages
        below map to p95 thresholds (for a 300ms SLA):
          margin >= 70%  -> p95 <= 90ms   double (+100%)
          margin >= 50%  -> p95 <= 150ms  +50%
          margin >= 40%  -> p95 <= 180ms  +40%
          margin >= 30%  -> p95 <= 210ms  +30%
          margin >= 20%  -> p95 <= 240ms  +20%
          margin >= 10%  -> p95 <= 270ms  +5%
          margin >= 5%   -> p95 <= 285ms  +2.5%
          margin <  5%   -> p95 >  285ms  +1% (usually clamped to min_step_users)
        """
        if margin_frac >= 0.70:
            return 1.00, "very-low-util"
        # We add half of the margin to the step, as we have a lot of headroom.
        if margin_frac >= 0.50:
            return 0.50, "comfortable"
        if margin_frac >= 0.40:
            return 0.40, "ample"
        if margin_frac >= 0.30:
            return 0.30, "moderate"
        if margin_frac >= 0.20:
            return 0.20, "tight"
        # We add a quarter of the margin to the step, as we are close to the edge.
        if margin_frac >= 0.10:
            return 0.05, "near-edge"
        if margin_frac >= 0.05:
            return 0.025, "edge"
        return 0.01, "at-edge"

    def _check_plateau(self) -> bool:
        n = int(self._p.plateau_stop_steps)
        if len(self._goodput_history) < n + 1:
            return False
        recent = [g for _u, g in self._goodput_history[-(n + 1):]]
        baseline = recent[0]
        if baseline <= 0:
            return False
        threshold = float(self._p.plateau_goodput_threshold_pct) / 100.0
        for g in recent[1:]:
            if (g - baseline) / baseline >= threshold:
                return False
        return True

    def _spawn_rate(self) -> int:
        return max(1, min(int(self._p.spawn_rate), int(self._step), int(self._users)))

    def _decide_and_advance(
        self,
        p95_ms: float,
        fail_pct: float,
        goodput_rps: float,
        stable: bool,
    ) -> str:
        sla = float(self._p.sla_ms)
        thr = float(self._p.failure_threshold_pct)
        sla_p95_ok = p95_ms <= sla
        sla_fail_ok = fail_pct <= thr
        below = sla_p95_ok and sla_fail_ok

        if below:
            self._low_ok = max(self._low_ok or 0, int(self._users))
            self._goodput_history.append((int(self._users), float(goodput_rps)))
            if self._check_plateau():
                self._done = True
                self._stop_reason = "goodput-plateau"
                return (
                    f"plateau (last {self._p.plateau_stop_steps} steps grew goodput "
                    f"< {self._p.plateau_goodput_threshold_pct:g}%) stopping at "
                    f"users={self._users}"
                )
        else:
            self._high_bad = (
                int(self._users)
                if self._high_bad is None
                else min(self._high_bad, int(self._users))
            )

        # Bracket refinement once we have both endpoints.
        if (
            self._low_ok is not None
            and self._high_bad is not None
            and self._high_bad > self._low_ok
        ):
            gap = int(self._high_bad - self._low_ok)
            if gap <= int(self._p.min_step_users):
                self._done = True
                self._stop_reason = "bracket-narrow"
                return (
                    f"bracket narrow (low={self._low_ok} high={self._high_bad}); "
                    f"stopping at users={self._users}"
                )
            self._step = max(
                int(self._p.min_step_users),
                min(int(self._p.max_step_users), int(gap // 2)),
            )
            self._users = min(
                int(self._p.max_users), int(self._low_ok + self._step)
            )
            return (
                f"bracket refine low={self._low_ok} high={self._high_bad} -> "
                f"users={self._users} step={self._step}"
            )

        prev_users = int(self._users)
        if below:
            margin_frac = max(0.0, (sla - p95_ms) / sla) if sla > 0 else 0.0
            factor, band = self._ramp_factor(margin_frac)
            # Step is a fraction of the CURRENT user count (not an accumulator),
            # so it self-scales and shrinks as p95 nears the SLA.
            self._step = max(
                int(self._p.min_step_users),
                min(int(self._p.max_step_users), int(round(self._users * factor))),
            )
            if int(self._users) >= int(self._p.max_users):
                self._done = True
                self._stop_reason = "max-users-reached"
                return f"max users ceiling hit at users={self._users}; stopping"
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            stab_note = "" if stable else " unstable-cap"
            return (
                f"below SLA (p95={p95_ms:.0f}ms margin={margin_frac * 100:.0f}% "
                f"band={band} fail%={fail_pct:.1f}) ramp +{factor:.0%}{stab_note} -> "
                f"users={self._users} step={self._step}"
            )

        reasons: list[str] = []
        if not sla_p95_ok:
            reasons.append(f"p95={p95_ms:.0f}>SLA={sla:.0f}")
        if not sla_fail_ok:
            reasons.append(f"fail%={fail_pct:.1f}>thr={thr:.1f}")
        self._step = max(int(self._p.min_step_users), int(self._step // 2))
        new_users = max(0, int(self._users - self._step))
        if new_users <= 0:
            self._done = True
            self._stop_reason = "sla-floor"
            return (
                f"SLA breach ({' '.join(reasons)}) at minimum users={prev_users}; "
                "cannot back off further; stopping"
            )
        self._users = new_users
        return (
            f"SLA breach ({' '.join(reasons)}) backoff -> users={self._users} "
            f"step={self._step}"
        )

    def _emit_final(self) -> None:
        if self._final_logged or self._stop_reason is None:
            return
        self._final_logged = True
        history = ", ".join(
            f"{u}u:{g:.1f}/s" for u, g in self._goodput_history[-8:]
        ) or "(none)"
        _LOG.info(
            "adaptive-v2 stop: reason=%s final_users=%s low_ok=%s high_bad=%s "
            "goodput_history=[%s]",
            self._stop_reason,
            self._users,
            self._low_ok,
            self._high_bad,
            history,
        )

    def tick(self):
        if self._should_stop():
            if self._stop_reason is None:
                self._stop_reason = "run-time-elapsed"
            self._emit_final()
            return None
        if self._done:
            self._emit_final()
            return None

        t = float(self.get_run_time())
        if self._level_start_t <= 0.0:
            self._enter_level(t)

        if self._should_abort_no_healthy_users(t):
            self._emit_final()
            return None

        # Sample latency periodically after the per-step trim window.
        if t >= self._next_sample_t:
            if (t - self._level_start_t) >= float(self._p.trim_s):
                v = self._read_latency_ms()
                if v is not None and v > 0:
                    self._lat_samples.append(v)
                    _LOG.info(
                        "adaptive p95 sample t=%.0fs users=%s p95=%.0fms",
                        t,
                        self._users,
                        v,
                    )
            self._next_sample_t = t + float(self._p.sample_every_s)

        elapsed = t - self._level_start_t

        # Warm-up step: hold start_users, discard everything observed.
        if self._is_warmup:
            if elapsed >= float(self._p.warmup_step_duration_s):
                _LOG.info(
                    "adaptive-v2 warmup end t=%.0fs at users=%s | %s",
                    t,
                    self._users,
                    self._stats_snapshot(),
                )
                self._is_warmup = False
                self._enter_level(t)
            return int(self._users), self._spawn_rate()

        if elapsed < float(self._p.min_step_duration_s):
            return int(self._users), self._spawn_rate()

        samples = self._lat_samples
        recent = samples[-int(self._p.min_settle_samples):]
        have_enough = len(recent) >= int(self._p.min_settle_samples)
        drift_pct = _latency_drift_pct(recent)
        stable = drift_pct <= float(self._p.stability_drift_threshold_pct)

        at_cap = elapsed >= float(self._p.max_step_duration_s)
        if not have_enough and not at_cap:
            return int(self._users), self._spawn_rate()
        if have_enough and not stable and not at_cap:
            return int(self._users), self._spawn_rate()

        if not have_enough:
            _LOG.warning(
                "adaptive phase end t=%.0fs: insufficient samples (%d/%d) at users=%s "
                "after %ds | %s",
                t,
                len(recent),
                int(self._p.min_settle_samples),
                self._users,
                int(elapsed),
                self._stats_snapshot(),
            )
            self._enter_level(t)
            return int(self._users), self._spawn_rate()

        p95_ms = statistics.mean(recent)
        reqs_now, fails_now = self._totals()
        delta_reqs = max(0, reqs_now - self._level_start_reqs)
        delta_fails = max(0, fails_now - self._level_start_fails)
        fail_pct = (100.0 * delta_fails / delta_reqs) if delta_reqs > 0 else 0.0
        # Use the full step duration for goodput: ``delta_reqs`` spans the entire
        # level window (including the trim cool-in), not just the post-trim slice.
        measured_s = max(1.0, float(elapsed))
        success_rps = max(0.0, (delta_reqs - delta_fails) / measured_s)

        action = self._decide_and_advance(
            p95_ms=float(p95_ms),
            fail_pct=float(fail_pct),
            goodput_rps=float(success_rps),
            stable=bool(stable),
        )
        _LOG.info(
            "adaptive phase end t=%.0fs: %s | %s | step_goodput=%.1f/s drift=%.1f%% "
            "step_reqs=%d step_fail=%d samples=%s",
            t,
            action,
            self._stats_snapshot(),
            success_rps,
            drift_pct,
            delta_reqs,
            delta_fails,
            [round(v, 1) for v in recent],
        )
        if self._done:
            self._emit_final()
            return None
        self._enter_level(t)
        return int(self._users), self._spawn_rate()


@dataclass(frozen=True)
class _GoodputPlateauParams:
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

    spawn_target_duration_s: float
    spawn_settle_buffer_s: float

    health_grace_s: int
    abort_on_no_users: bool


def _goodput_plateau_params_from_manifest() -> _GoodputPlateauParams:
    cfg = _load_manifest()
    mode = (cfg.get("mode") or "").strip().lower()
    if mode == "explore_refine":
        _validate_explore_refine_manifest(cfg)
        min_step_s = max(5, int(_manifest_required(cfg, "refine_min_step_duration_s")))
        max_step_s = max(
            min_step_s,
            int(_manifest_required(cfg, "refine_max_step_duration_s")),
        )
        trim_s = max(0, int(_manifest_required(cfg, "refine_measure_window_s")))
        warmup_s = max(0, int(_manifest_required(cfg, "explore_warmup_duration_s")))
        min_settle = max(2, int(_manifest_required(cfg, "refine_min_settle_samples")))
        plateau_stop = max(1, int(_manifest_required(cfg, "refine_stop_steps")))
        min_step_users = max(1, int(_manifest_required(cfg, "refine_min_step_users")))
        max_step_users = max(1, int(_manifest_required(cfg, "refine_max_step_users")))
        overload_backoff = max(
            1, int(_manifest_required(cfg, "refine_overload_backoff_max"))
        )
        drain_s = 0
        step_up_gain = max(1.0, float(_manifest_required(cfg, "refine_step_growth")))
        efficiency_good_threshold = max(
            0.0,
            min(1.0, float(_manifest_required(cfg, "refine_efficiency_good_threshold"))),
        )
        plateau_goodput_threshold_pct = float(
            _manifest_required(cfg, "collapse_threshold_pct")
        )
    else:
        trim_s = max(0, int(cfg["trim_s"]))
        min_step_s = max(5, int(cfg["min_step_duration_s"]))
        max_step_s = max(min_step_s, int(cfg["max_step_duration_s"]))
        warmup_s = max(0, int(cfg["warmup_step_duration_s"]))
        min_settle = max(2, int(cfg["min_settle_samples"]))
        plateau_stop = max(
            1,
            int(cfg.get("plateau_stop_steps", cfg.get("refine_stop_steps", 2))),
        )
        min_step_users = max(1, int(cfg["min_step_users"]))
        max_step_users = max(1, int(cfg["max_step_users"]))
        overload_backoff = max(1, int(cfg.get("overload_backoff_max", 2)))
        drain_s = max(0, int(cfg["drain_time_s"]))
        step_up_gain = max(1.0, float(cfg.get("step_up_gain", 1.5)))
        efficiency_good_threshold = max(
            0.0, min(1.0, float(cfg.get("efficiency_good_threshold", 0.95)))
        )
        plateau_goodput_threshold_pct = float(
            cfg.get("plateau_goodput_threshold_pct", 5.0)
        )
    return _GoodputPlateauParams(
        failure_threshold_pct=float(cfg["failure_threshold_pct"]),
        collapse_threshold_pct=float(cfg["collapse_threshold_pct"]),
        overload_p95_ms=float(cfg["overload_p95_ms"]),
        start_users=max(1, int(cfg["start_users"])),
        max_users=max(1, int(cfg["max_users"])),
        min_step_users=min_step_users,
        max_step_users=max_step_users,
        step_up_gain=step_up_gain,
        efficiency_good_threshold=efficiency_good_threshold,
        drain_time_s=drain_s,
        spawn_rate=max(1, int(cfg["spawn_rate"])),
        warmup_step_duration_s=warmup_s,
        min_step_duration_s=min_step_s,
        max_step_duration_s=max_step_s,
        trim_s=trim_s,
        sample_every_s=max(1, int(cfg["sample_every_s"])),
        min_settle_samples=min_settle,
        quantile=float(cfg["quantile"]),
        stability_drift_threshold_pct=float(cfg["stability_drift_threshold_pct"]),
        plateau_stop_steps=plateau_stop,
        plateau_goodput_threshold_pct=plateau_goodput_threshold_pct,
        overload_backoff_max=overload_backoff,
        spawn_target_duration_s=max(1.0, float(cfg["spawn_target_duration_s"])),
        spawn_settle_buffer_s=max(0.0, float(cfg["spawn_settle_buffer_s"])),
        health_grace_s=max(5, int(cfg["health_grace_s"])),
        abort_on_no_users=bool(cfg["abort_on_no_users"]),
    )


class GoodputPlateauShape(_BaseShape):
    """
    Goodput plateau controller (simplified).

    Per level, after trim (+ optional drain):
    - Sample rolling ~10s goodput and p95 every ``sample_every_s``.
    - Once ``min_settle_samples`` readings exist and p95 spread in that window is
      ≤ ``stability_drift_threshold_pct``, end the step (or at ``max_step_duration_s``).
    - ``step_goodput`` at phase end = mean of trailing decision-window goodput samples.
    - Ramp: first stable step jumps by ``max_step_users``; later steps use marginal
      goodput efficiency — high efficiency (≥ ``efficiency_good_threshold``) scales
      the last step by ``step_up_gain``, low efficiency scales by eff³. Holds when
      the computed step is below ``min_step_users``. After overload backoff, one
      conservative ``min_step_users`` recovery ramp.
    - Overload (fail% / p95 / stable goodput collapse): back off up to
      ``overload_backoff_max`` times, then stop if a stable best goodput exists.
    - Stall stop: ``plateau_stop_steps`` consecutive *passing* steps (stable or
      unstable-cap) that do not beat the best stable goodput seen so far (strict).

    Emits the same log grammar as AdaptiveV2Shape so existing parsing/plots work.
    """

    def __init__(self):
        super().__init__()
        self._p = _goodput_plateau_params_from_manifest()
        self._users = int(self._p.start_users)
        self._step = int(self._p.max_step_users)
        self._last_ramp_step = 0
        self._backoff_streak = 0
        self._efficiency_reset = False
        self._pending_drain_s = 0
        self._level_drain_s = 0
        self._level_start_t = 0.0
        self._next_sample_t = 0.0

        # Rolling goodput and p95 samples in the settle window (after trim+drain).
        self._lat_samples: list[float] = []
        self._goodput_samples: list[float] = []

        self._level_start_reqs = 0
        self._level_start_fails = 0
        self._window_start_reqs: int | None = None
        self._window_start_fails: int | None = None
        self._sample_ticks: list[tuple[float, int, int]] = []
        self._is_warmup = self._p.warmup_step_duration_s > 0

        self._goodput_history: list[tuple[int, float]] = []  # (users, level goodput rps)
        self._best_goodput: float = 0.0
        self._stall_count = 0

        self._low_ok: int | None = None
        self._high_bad: int | None = None
        self._done = False
        self._stop_reason: str | None = None
        self._final_logged = False
        self._abort_logged = False
        self._window_unavailable_logged = False

        _LOG.info(
            "adaptive-v2 start: users=%s spawn_rate=%s sla_ms=%s failure_thr_pct=%s "
            "warmup_s=%s step_duration_s=[%s..%s] trim_s=%s quantile=%s "
            "stability_drift_pct=%s stall_stop_steps=%s overload_backoff_max=%s "
            "step_up_gain=%s eff_good_thr=%s drain_s=%s max_step_users=%s max_users=%s",
            self._users,
            self._p.spawn_rate,
            "n/a",
            self._p.failure_threshold_pct,
            self._p.warmup_step_duration_s,
            self._p.min_step_duration_s,
            self._p.max_step_duration_s,
            self._p.trim_s,
            self._p.quantile,
            self._p.stability_drift_threshold_pct,
            self._p.plateau_stop_steps,
            self._p.overload_backoff_max,
            self._p.step_up_gain,
            self._p.efficiency_good_threshold,
            self._p.drain_time_s,
            self._p.max_step_users,
            self._p.max_users,
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

    def _totals(self) -> tuple[int, int]:
        env = self._locust_environment()
        total = getattr(getattr(env, "stats", None), "total", None) if env else None
        if total is None:
            return 0, 0
        reqs = int(getattr(total, "num_requests", 0) or 0)
        fails = int(getattr(total, "num_failures", 0) or 0)
        return reqs, fails

    def _current_rates(self) -> tuple[float, float] | None:
        """(current_rps, current_fail_per_sec) from Locust's trailing window.

        Locust deliberately averages over a trailing slice of per-second buckets
        and excludes the most recent ~2 seconds, which reduces bias from partial
        buckets and worker→master flush jitter in distributed mode.
        """
        env = self._locust_environment()
        total = getattr(getattr(env, "stats", None), "total", None) if env else None
        if total is None:
            return None
        try:
            rps = float(getattr(total, "current_rps", 0.0) or 0.0)
            failps = float(getattr(total, "current_fail_per_sec", 0.0) or 0.0)
        except Exception:
            return None
        return rps, failps

    def _read_latency_ms(self) -> float | None:
        env = self._locust_environment()
        total = getattr(getattr(env, "stats", None), "total", None) if env else None
        if total is None:
            return None
        v = None
        try:
            v = total.get_current_response_time_percentile(float(self._p.quantile))
        except Exception:
            v = None
        if v is None:
            if not self._window_unavailable_logged:
                self._window_unavailable_logged = True
                _LOG.warning(
                    "adaptive-v2: windowed p95 unavailable; falling back to "
                    "cumulative percentile (per-step latency may be polluted by history)"
                )
            try:
                v = total.get_response_time_percentile(float(self._p.quantile))
            except Exception:
                return None
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    def _stats_snapshot(self) -> str:
        reqs, fails = self._totals()
        fail_pct = f"{100.0 * fails / reqs:.1f}%" if reqs else "n/a"
        lat = self._read_latency_ms()
        lat_s = f"{lat:.0f}ms" if lat is not None else "n/a"
        return f"reqs={reqs} fail={fails} ({fail_pct}) p{int(self._p.quantile * 100)}={lat_s}"

    def _should_abort_no_healthy_users(self, t: float) -> bool:
        if not self._p.abort_on_no_users:
            return False
        if int(self._users) <= 0:
            return False
        if (t - self._level_start_t) < float(self._p.health_grace_s):
            return False
        active = self._active_user_count()
        if active is None or active > 0:
            return False
        if not self._abort_logged:
            self._abort_logged = True
            _LOG.error(
                "adaptive-v2 abort at t=%.0fs: zero active users (target=%s, grace=%ss); %s",
                t,
                self._users,
                self._p.health_grace_s,
                self._stats_snapshot(),
            )
        self._stop_reason = "no-healthy-users"
        self._done = True
        return True

    def _enter_level(self, t: float) -> None:
        prev_target = int(getattr(self, "_prev_level_users", self._users))
        signed_delta = int(self._users) - prev_target
        self._level_signed_delta = signed_delta
        self._level_delta_users = max(0, signed_delta)
        self._prev_level_users = int(self._users)
        self._level_start_t = t
        self._next_sample_t = t
        self._level_drain_s = int(self._pending_drain_s)
        self._pending_drain_s = 0
        self._lat_samples = []
        self._goodput_samples = []
        reqs, fails = self._totals()
        self._level_start_reqs = reqs
        self._level_start_fails = fails
        self._window_start_reqs = None
        self._window_start_fails = None
        self._sample_ticks = []
        self._abort_logged = False
        if self._level_delta_users > 0:
            settle_s = self._spawn_settle_s()
            _LOG.info(
                "adaptive level ramp +%s -> users=%s spawn_rate=%s "
                "spawn_settle_s=%.1f measure_start_s=%.1f",
                self._level_delta_users,
                self._users,
                self._spawn_rate(),
                settle_s,
                self._measure_start_s(),
            )

    def _spawn_settle_s(self) -> float:
        """Seconds to wait after a ramp-up before measuring (spawn + buffer)."""
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta <= 0:
            return 0.0
        rate = max(1, int(self._spawn_rate()))
        return float(delta) / float(rate) + float(self._p.spawn_settle_buffer_s)

    def _spawn_complete(self, t: float) -> bool:
        """True once ramp-up users should be active (time + active count)."""
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta <= 0:
            return True
        elapsed = t - self._level_start_t
        if elapsed < self._spawn_settle_s():
            return False
        active = self._active_user_count()
        if active is None:
            return True
        target = int(self._users)
        tolerance = max(2, int(0.02 * target))
        return active >= target - tolerance

    def _decision_window_size(self) -> int:
        return int(self._p.min_settle_samples)

    def _level_goodput_from_window(self) -> float:
        """
        Mean successful RPS over the settle window.

        Uses request-counter boundaries (not the mean of per-tick deltas) so
        bursty Locust master stat updates do not insert false zero intervals.
        """
        n = self._decision_window_size()
        ticks = self._sample_ticks[-(n + 1) :]
        if len(ticks) < 2:
            return 0.0
        t0, r0, f0 = ticks[0]
        t1, r1, f1 = ticks[-1]
        dt = max(0.001, float(t1) - float(t0))
        succ0 = max(0, int(r0) - int(f0))
        succ1 = max(0, int(r1) - int(f1))
        return max(0.0, float(succ1 - succ0) / dt)

    def _level_window_counts(self) -> tuple[int, int]:
        """(requests, failures) over the trailing decision window.

        Uses the same last ``n+1`` sample-tick counter boundaries as
        ``_level_goodput_from_window`` so fail% and goodput cover the same ~10s
        slice of time instead of goodput being windowed while fail% spans the
        whole post-trim level.
        """
        n = self._decision_window_size()
        ticks = self._sample_ticks[-(n + 1):]
        if len(ticks) < 2:
            return 0, 0
        _t0, r0, f0 = ticks[0]
        _t1, r1, f1 = ticks[-1]
        return max(0, int(r1) - int(r0)), max(0, int(f1) - int(f0))

    def _measure_start_s(self) -> float:
        spawn_wait = self._spawn_settle_s()
        base_trim = max(float(self._p.trim_s), spawn_wait)
        return base_trim + float(self._level_drain_s)

    def _min_level_duration_s(self) -> float:
        measure_s = self._measure_start_s() - float(self._level_drain_s)
        window_s = float(self._p.min_settle_samples) * float(self._p.sample_every_s)
        min_hold = max(float(self._p.min_step_duration_s), measure_s + window_s)
        return min_hold + float(self._level_drain_s)

    def _max_level_duration_s(self) -> float:
        measure_s = self._measure_start_s() - float(self._level_drain_s)
        window_s = float(self._p.min_settle_samples) * float(self._p.sample_every_s)
        needed = measure_s + window_s + 5.0
        return max(float(self._p.max_step_duration_s), needed) + float(self._level_drain_s)

    def _spawn_rate(self) -> int:
        ceiling = int(self._p.spawn_rate)
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta <= 0:
            return max(1, min(ceiling, int(self._users)))
        target_dur = float(self._p.spawn_target_duration_s)
        rate_for_step = max(1, int(math.ceil(delta / target_dur)))
        return max(1, min(ceiling, rate_for_step, int(self._users)))

    def _clamp_step_users(self, step_users: int) -> int:
        return max(
            int(self._p.min_step_users),
            min(int(self._p.max_step_users), int(step_users)),
        )

    def _apply_ramp_step(self, raw_step: int) -> int:
        step_users = self._clamp_step_users(raw_step)
        self._step = step_users
        self._last_ramp_step = step_users
        return step_users

    def _check_stall(self, goodput_rps: float, *, stable: bool) -> str | None:
        """Count passing steps that fail to beat the best stable goodput."""
        improved = float(goodput_rps) > float(self._best_goodput)
        if improved:
            self._stall_count = 0
            return None
        self._stall_count += 1
        n = int(self._p.plateau_stop_steps)
        if self._stall_count < n:
            return None
        self._done = True
        self._stop_reason = "goodput-stall"
        stab_note = "" if stable else " incl-unstable-cap"
        return (
            f"stall ({self._stall_count} consecutive steps without "
            f"goodput improvement{stab_note}) stopping at users={self._users}"
        )

    def _record_stable_step(self, goodput_rps: float) -> None:
        """Persist a settled reading for efficiency, collapse, and reporting."""
        self._goodput_history.append((int(self._users), float(goodput_rps)))
        self._best_goodput = max(self._best_goodput, float(goodput_rps))

    @staticmethod
    def _goodput_efficiency(
        prev_users: int, prev_goodput: float, users: int, goodput: float
    ) -> float:
        if prev_users <= 0 or prev_goodput <= 0:
            return 0.0
        if users <= prev_users:
            return 0.0
        d_goodput_frac = (goodput - prev_goodput) / prev_goodput
        d_users_frac = (users - prev_users) / prev_users
        if d_users_frac <= 0:
            return 0.0
        return d_goodput_frac / d_users_frac

    def _emit_final(self) -> None:
        if self._final_logged or self._stop_reason is None:
            return
        self._final_logged = True
        history = ", ".join(f"{u}u:{g:.1f}/s" for u, g in self._goodput_history[-8:]) or "(none)"
        _LOG.info(
            "adaptive-v2 stop: reason=%s final_users=%s low_ok=%s high_bad=%s goodput_history=[%s]",
            self._stop_reason,
            self._users,
            self._low_ok,
            self._high_bad,
            history,
        )

    def _backoff_drop_users(self) -> int:
        """How many users to subtract on this overload backoff.

        First backoff undoes the last ramp step; each subsequent consecutive
        backoff scales the drop by ``step_up_gain`` (symmetric to ramp-up).
        """
        base = int(self._last_ramp_step) or int(self._step) or int(self._p.min_step_users)
        mult = float(self._p.step_up_gain) ** int(self._backoff_streak)
        drop = int(round(base * mult))
        return max(
            int(self._p.min_step_users),
            min(int(self._p.max_step_users), drop),
        )

    def _backoff(self, *, reason: str) -> str:
        prev_users = int(self._users)
        self._high_bad = (
            int(self._users)
            if self._high_bad is None
            else min(self._high_bad, int(self._users))
        )
        drop = self._backoff_drop_users()
        self._backoff_streak += 1
        new_users = max(0, int(self._users - drop))
        if new_users <= 0:
            self._done = True
            self._stop_reason = "goodput-floor"
            return (
                f"overload ({reason}) at minimum users={prev_users}; "
                "cannot back off further; stopping"
            )
        self._users = new_users
        self._step = int(self._p.min_step_users)
        self._efficiency_reset = True
        self._pending_drain_s = int(self._p.drain_time_s)
        return (
            f"overload ({reason}) backoff -{drop} (streak={self._backoff_streak}) "
            f"-> users={self._users} step={self._step} drain_s={self._p.drain_time_s}"
        )

    def _decide_and_advance(
        self, *, fail_pct: float, goodput_rps: float, stable: bool, p95_ms: float | None
    ) -> str:
        thr = float(self._p.failure_threshold_pct)
        collapse_thr = float(self._p.collapse_threshold_pct) / 100.0

        overloaded_reasons: list[str] = []
        if fail_pct > thr:
            overloaded_reasons.append(f"fail%={fail_pct:.1f}>thr={thr:.1f}")
        if p95_ms is not None and p95_ms > float(self._p.overload_p95_ms):
            overloaded_reasons.append(f"p95={p95_ms:.0f}>{float(self._p.overload_p95_ms):.0f}ms")
        # Goodput collapse only on stable steps: compare this level's settled
        # ~10s window against the best stable goodput recorded so far.
        if (
            stable
            and self._best_goodput > 0
            and goodput_rps < (1.0 - collapse_thr) * self._best_goodput
        ):
            overloaded_reasons.append(
                f"goodput={goodput_rps:.1f}<{(1.0 - collapse_thr) * self._best_goodput:.1f}"
            )

        if overloaded_reasons:
            reason = " ".join(overloaded_reasons)
            if (
                self._backoff_streak >= int(self._p.overload_backoff_max)
                and self._best_goodput > 0
            ):
                self._done = True
                self._stop_reason = "overload-peak"
                return (
                    f"overload ({reason}) after {self._backoff_streak} backoffs; "
                    f"stopping at users={self._users} "
                    f"best_goodput={self._best_goodput:.1f}/s"
                )
            return self._backoff(reason=reason)

        # Passing step.
        self._backoff_streak = 0
        self._low_ok = max(self._low_ok or 0, int(self._users))

        last_stable = self._goodput_history[-1] if self._goodput_history else None

        stall_stop = self._check_stall(float(goodput_rps), stable=bool(stable))
        if stall_stop is not None:
            return stall_stop

        if stable:
            self._record_stable_step(float(goodput_rps))

        if int(self._users) >= int(self._p.max_users):
            self._done = True
            self._stop_reason = "max-users-reached"
            return f"max users ceiling hit at users={self._users}; stopping"

        if last_stable is None:
            self._apply_ramp_step(int(self._p.max_step_users))
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            stab_note = "" if stable else " unstable-cap"
            return f"goodput ramp init{stab_note} -> users={self._users} step={self._step}"

        if self._efficiency_reset:
            self._efficiency_reset = False
            self._apply_ramp_step(int(self._p.min_step_users))
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            stab_note = "" if stable else " unstable-cap"
            return f"goodput ramp recovery{stab_note} -> users={self._users} step={self._step}"

        prev_users, prev_goodput = last_stable
        eff = self._goodput_efficiency(
            prev_users, prev_goodput, int(self._users), float(goodput_rps)
        )
        base_step = float(self._last_ramp_step or self._step or self._p.min_step_users)
        eff_thr = float(self._p.efficiency_good_threshold)
        if eff >= eff_thr:
            band = "high-eff"
            factor = float(self._p.step_up_gain)
            uncapped = int(round(base_step * factor))
        else:
            band = "low-eff"
            e = max(0.0, min(1.0, float(eff)))
            factor = e * e * e
            if factor <= 0:
                return (
                    f"goodput hold eff={eff:.2f} band={band} factor={factor:.3f} "
                    f"fail%={fail_pct:.1f} -> users={self._users} step=0"
                )
            uncapped = int(round(base_step * factor))
        if uncapped < int(self._p.min_step_users):
            return (
                f"goodput hold eff={eff:.2f} band={band} uncapped={uncapped}"
                f"<min={self._p.min_step_users} fail%={fail_pct:.1f} -> "
                f"users={self._users} step=0"
            )
        self._apply_ramp_step(uncapped)
        self._users = min(int(self._p.max_users), int(self._users + self._step))
        stab_note = "" if stable else " unstable-cap"
        return (
            f"goodput ramp eff={eff:.2f} band={band} factor={factor:.3f} "
            f"fail%={fail_pct:.1f}{stab_note} -> users={self._users} step={self._step}"
        )

    def tick(self):
        if self._should_stop():
            if self._stop_reason is None:
                self._stop_reason = "run-time-elapsed"
            self._emit_final()
            return None
        if self._done:
            self._emit_final()
            return None

        t = float(self.get_run_time())
        if self._level_start_t <= 0.0:
            self._enter_level(t)

        if self._should_abort_no_healthy_users(t):
            self._emit_final()
            return None

        if not self._spawn_complete(t):
            return int(self._users), self._spawn_rate()

        # Sample p95 and rolling ~10s goodput after trim (+ drain), not during spawn-in.
        if t >= self._next_sample_t:
            elapsed = t - self._level_start_t
            if elapsed >= self._measure_start_s():
                reqs_now, fails_now = self._totals()
                if self._window_start_reqs is None:
                    self._window_start_reqs = reqs_now
                    self._window_start_fails = fails_now
                    self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
                    v = self._read_latency_ms()
                    if v is not None and v > 0:
                        self._lat_samples.append(v)
                else:
                    # Append the current tick first so the trailing goodput
                    # window computed below includes it.
                    self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
                    v = self._read_latency_ms()
                    if v is not None and v > 0:
                        self._lat_samples.append(v)

                    # Log the trailing ~10s rolling goodput (identical metric to
                    # the one fed into the ramp decision) rather than the bursty
                    # single-tick counter delta, which is distorted by batched
                    # Locust master stat updates. Plots read these sample lines,
                    # so this keeps the plotted curve and the decision on one
                    # source of truth.
                    roll_goodput = self._level_goodput_from_window()
                    # For *sample logging/plotting*, prefer Locust's current_rps
                    # which is already a trailing-window mean and ignores the
                    # most recent ~2 seconds. This makes the sample dots line up
                    # with stats_history.csv and reduces visible "striping" from
                    # bursty worker flushes.
                    rates = self._current_rates()
                    if rates is not None:
                        rps, failps = rates
                        roll_goodput = max(0.0, rps - failps)
                        win_fail_pct = (100.0 * failps / rps) if rps > 0 else 0.0
                    else:
                        self._goodput_samples.append(float(roll_goodput))
                        win_reqs, win_fails = self._level_window_counts()
                        win_fail_pct = (100.0 * win_fails / win_reqs) if win_reqs > 0 else 0.0
                    self._goodput_samples.append(float(roll_goodput))
                    if v is not None and v > 0:
                        _LOG.info(
                            "adaptive sample t=%.0fs users=%s goodput=%.1f/s fail_pct=%.2f%% p95=%.0fms",
                            t,
                            self._users,
                            roll_goodput,
                            win_fail_pct,
                            v,
                        )

            self._next_sample_t = t + float(self._p.sample_every_s)

        elapsed = t - self._level_start_t

        # Warm-up step: hold start_users, discard everything observed.
        if self._is_warmup:
            if elapsed >= float(self._p.warmup_step_duration_s):
                _LOG.info("adaptive-v2 warmup end t=%.0fs at users=%s | %s", t, self._users, self._stats_snapshot())
                self._is_warmup = False
                self._enter_level(t)
            return int(self._users), self._spawn_rate()

        if elapsed < self._min_level_duration_s():
            return int(self._users), self._spawn_rate()

        n = self._decision_window_size()
        recent_lat = self._lat_samples[-n:]
        recent_ticks = self._sample_ticks[-(n + 1) :]
        have_enough = len(recent_lat) >= n and len(recent_ticks) >= n + 1
        p95_spread_pct = _sample_spread_pct(recent_lat)
        stable = p95_spread_pct <= float(self._p.stability_drift_threshold_pct)

        at_cap = elapsed >= self._max_level_duration_s()
        if not have_enough and not at_cap:
            return int(self._users), self._spawn_rate()
        if have_enough and not stable and not at_cap:
            return int(self._users), self._spawn_rate()

        if not have_enough:
            _LOG.warning(
                "adaptive phase end t=%.0fs: insufficient decision-window samples "
                "(p95=%d/%d ticks=%d/%d) at users=%s after %ds | %s",
                t,
                len(recent_lat),
                n,
                len(recent_ticks),
                n + 1,
                self._users,
                int(elapsed),
                self._stats_snapshot(),
            )
            self._enter_level(t)
            return int(self._users), self._spawn_rate()

        # Goodput used for decisions should match the "adaptive sample" dots and
        # Locust stats_history (current_rps/current_fail_per_sec). Using the
        # mean of the trailing decision-window samples also reduces sensitivity
        # to bursty worker→master flush timing in distributed mode.
        recent_goodput = self._goodput_samples[-n:]
        level_goodput_rps = (
            float(statistics.mean(recent_goodput))
            if recent_goodput
            else float(self._level_goodput_from_window())
        )
        # Failure rate over the SAME trailing decision window as goodput (last
        # ~10s), so fail% and goodput describe the same slice of time.
        win_reqs, win_fails = self._level_window_counts()
        fail_pct = (100.0 * win_fails / win_reqs) if win_reqs > 0 else 0.0
        step_p95_ms = max(recent_lat) if recent_lat else None
        action = self._decide_and_advance(
            fail_pct=float(fail_pct),
            goodput_rps=float(level_goodput_rps),
            stable=bool(stable),
            p95_ms=step_p95_ms,
        )
        _LOG.info(
            "adaptive phase end t=%.0fs: %s | %s | step_goodput=%.1f/s "
            "p95_spread=%.1f%% drift=%.1f%% step_reqs=%d step_fail=%d "
            "goodput_window=%s samples=%s",
            t,
            action,
            self._stats_snapshot(),
            level_goodput_rps,
            p95_spread_pct,
            p95_spread_pct,
            win_reqs,
            win_fails,
            [round(v, 1) for v in recent_goodput],
            [round(v, 1) for v in recent_lat],
        )
        if self._done:
            self._emit_final()
            return None
        self._enter_level(t)
        return int(self._users), self._spawn_rate()


class ExploreRefineShape(GoodputPlateauShape):
    """
    Three-phase goodput finder: explore ramp, settle-floor recovery, fine refine.

    Explore phase bumps users by ``explore_ramp_user_fraction_per_s`` on a P95-adaptive
    interval: ``max(1s, ceil(p95_ms / 100ms))``. Recovery jumps to ``recovery_floor_fraction`` × explore peak users, holds for
    ``recovery_settle_duration_s``, then checks p95/fail stability. On failure it
    drops by ``recovery_retry_drop_fraction`` and settles again before refine.
    """

    def __init__(self):
        super().__init__()
        cfg = _load_manifest()
        _validate_explore_refine_manifest(cfg)
        self._explore_warmup_duration_s = max(
            0, int(_manifest_required(cfg, "explore_warmup_duration_s"))
        )
        self._is_warmup = self._explore_warmup_duration_s > 0
        self._explore_ramp_fraction = max(
            0.001,
            min(1.0, float(_manifest_required(cfg, "explore_ramp_user_fraction_per_s"))),
        )
        self._explore_min_step_users = max(
            1, int(_manifest_required(cfg, "explore_min_step_users"))
        )
        self._explore_stop_ratio = max(
            0.5,
            min(1.0, float(_manifest_required(cfg, "explore_goodput_stop_ratio"))),
        )
        self._explore_stop_steps = max(
            1, int(_manifest_required(cfg, "explore_stop_steps"))
        )
        self._recovery_floor_fraction = max(
            0.1,
            min(1.0, float(_manifest_required(cfg, "recovery_floor_fraction"))),
        )
        self._recovery_settle_duration_s = max(
            5, int(_manifest_required(cfg, "recovery_settle_duration_s"))
        )
        self._recovery_retry_drop_fraction = max(
            0.01,
            min(1.0, float(_manifest_required(cfg, "recovery_retry_drop_fraction"))),
        )
        self._refine_min_step_duration_s = max(
            5, int(_manifest_required(cfg, "refine_min_step_duration_s"))
        )
        self._refine_max_step_duration_s = max(
            self._refine_min_step_duration_s,
            int(_manifest_required(cfg, "refine_max_step_duration_s")),
        )
        self._refine_min_settle_samples = max(
            2, int(_manifest_required(cfg, "refine_min_settle_samples"))
        )
        self._refine_max_step_users = max(
            1, int(_manifest_required(cfg, "refine_max_step_users"))
        )
        self._refine_overload_backoff_max = max(
            1, int(_manifest_required(cfg, "refine_overload_backoff_max"))
        )
        self._refine_max_step_frac = float(
            _manifest_required(cfg, "refine_max_step_fraction")
        )
        self._refine_initial_step_frac = float(
            _manifest_required(cfg, "refine_initial_step_fraction")
        )
        self._refine_efficiency_good_threshold = max(
            0.0,
            min(
                1.0,
                float(_manifest_required(cfg, "refine_efficiency_good_threshold")),
            ),
        )
        self._refine_step_growth = max(
            1.0, float(_manifest_required(cfg, "refine_step_growth"))
        )
        self._refine_stop_steps = max(
            1, int(_manifest_required(cfg, "refine_stop_steps"))
        )
        self._refine_measure_window_s = max(
            1.0, float(_manifest_required(cfg, "refine_measure_window_s"))
        )
        self._refine_min_step_users = max(
            1, int(_manifest_required(cfg, "refine_min_step_users"))
        )
        self._phase = "explore"
        self._refine_max_step: int | None = None
        self._refine_min_step: int | None = None
        self._refine_initial_step: int | None = None
        self._refine_best_goodput: float = 0.0
        self._refine_stall_count = 0
        self._explore_peak_goodput: float = 0.0
        self._explore_peak_users: int | None = None
        self._explore_collapse_streak = 0
        self._explore_last_decision_t = 0.0
        self._explore_last_bump_t = 0.0
        self._recovery_peak_users: int | None = None
        self._recovery_settle_start_t = 0.0
        self._recovery_attempt = 0
        self._last_tick_t = 0.0
        _LOG.info(
            "explore-refine init: warmup=%ss ramp_frac=%.1f%%/step ramp_interval=p95/100ms "
            "explore_min_step=%s explore_stop=%.0f%% explore_checks=%s "
            "recovery_floor=%.0f%% recovery_settle=%ss recovery_retry_drop=%.0f%% "
            "refine_init=%.0f%% refine_max=%.0f%% refine_eff_thr=%.2f "
            "refine_growth=%.2f refine_measure_s=%.0f refine_settle=%s",
            self._explore_warmup_duration_s,
            100.0 * self._explore_ramp_fraction,
            self._explore_min_step_users,
            100.0 * self._explore_stop_ratio,
            self._explore_stop_steps,
            100.0 * self._recovery_floor_fraction,
            self._recovery_settle_duration_s,
            100.0 * self._recovery_retry_drop_fraction,
            100.0 * self._refine_initial_step_frac,
            100.0 * self._refine_max_step_frac,
            self._refine_efficiency_good_threshold,
            self._refine_step_growth,
            self._refine_measure_window_s,
            self._refine_min_settle_samples,
        )

    def _decision_window_size(self) -> int:
        if self._phase in ("refine", "recovery"):
            return int(self._refine_min_settle_samples)
        if self._phase == "explore":
            return max(2, min(5, int(self._refine_min_settle_samples)))
        return super()._decision_window_size()

    def _min_level_duration_s(self) -> float:
        if self._phase == "refine":
            measure_s = self._measure_start_s() - float(self._level_drain_s)
            window_s = float(self._refine_min_settle_samples) * float(self._p.sample_every_s)
            min_hold = max(float(self._refine_min_step_duration_s), measure_s + window_s)
            return min_hold + float(self._level_drain_s)
        if self._phase in ("explore", "recovery"):
            return 0.0
        return super()._min_level_duration_s()

    def _max_level_duration_s(self) -> float:
        if self._phase == "refine":
            measure_s = self._measure_start_s() - float(self._level_drain_s)
            window_s = float(self._refine_min_settle_samples) * float(self._p.sample_every_s)
            needed = measure_s + window_s + 5.0
            return max(float(self._refine_max_step_duration_s), needed) + float(
                self._level_drain_s
            )
        if self._phase in ("explore", "recovery"):
            return float(self._refine_max_step_duration_s)
        return super()._max_level_duration_s()

    def _backoff(self, *, reason: str) -> str:
        prev_users = int(self._users)
        self._high_bad = (
            int(self._users)
            if self._high_bad is None
            else min(self._high_bad, int(self._users))
        )
        drop = self._backoff_drop_users()
        self._backoff_streak += 1
        new_users = max(0, int(self._users - drop))
        if new_users <= 0:
            self._done = True
            self._stop_reason = "goodput-floor"
            return (
                f"overload ({reason}) at minimum users={prev_users}; "
                "cannot back off further; stopping"
            )
        self._users = new_users
        self._step = int(self._refine_min_step_users)
        self._efficiency_reset = True
        self._pending_drain_s = 0
        return (
            f"overload ({reason}) backoff -{drop} (streak={self._backoff_streak}) "
            f"-> users={self._users} step={self._step}"
        )

    def _clamp_step_users(self, step_users: int) -> int:
        if self._phase == "refine":
            lo = int(self._refine_min_step or self._refine_min_step_users)
            hi = int(self._refine_max_step or self._refine_max_step_users)
            return max(lo, min(hi, int(step_users)))
        return super()._clamp_step_users(step_users)

    def _ramp_spawn_caught_up(self) -> bool:
        """True when active users have reached the current shape target."""
        active = self._active_user_count()
        target = int(self._users)
        if active is None or target <= 0:
            return True
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta <= 0:
            return True
        tolerance = max(5, int(0.02 * target))
        return int(active) >= target - tolerance

    def _explore_ramp_interval_from_p95(self, p95_ms: float | None) -> float:
        """Seconds between explore bumps: 1s below 100ms p95, else ceil(p95/100ms)."""
        if p95_ms is None or float(p95_ms) < 100.0:
            return 1.0
        return float(max(1, int(math.ceil(float(p95_ms) / 100.0))))

    def _current_explore_p95_ms(self) -> float | None:
        if self._lat_samples:
            return float(self._lat_samples[-1])
        return None

    def _explore_step_users(self, current_users: int) -> int:
        return max(
            int(self._explore_min_step_users),
            int(round(int(current_users) * float(self._explore_ramp_fraction))),
        )

    def _sync_explore_users(self, t: float) -> int:
        prev = int(self._users)
        if prev >= int(self._p.max_users):
            return 0
        step = self._explore_step_users(prev)
        new_users = min(int(self._p.max_users), prev + step)
        if new_users <= prev:
            return 0
        self._prev_level_users = prev
        self._users = new_users
        self._level_delta_users = new_users - prev
        self._level_signed_delta = new_users - prev
        self._step = step
        self._last_ramp_step = step
        return step

    def _recovery_initial_floor_users(self, peak_users: int) -> int:
        floor = int(round(int(peak_users) * float(self._recovery_floor_fraction)))
        return max(int(self._p.start_users), floor)

    def _recovery_retry_users(self, current_users: int) -> int:
        drop_frac = float(self._recovery_retry_drop_fraction)
        retried = int(round(int(current_users) * (1.0 - drop_frac)))
        return max(int(self._p.start_users), retried)

    def _recovery_set_user_level(self, new_users: int) -> int:
        prev = int(self._users)
        new_users = max(int(self._p.start_users), int(new_users))
        if new_users == prev:
            return 0
        self._prev_level_users = prev
        self._users = new_users
        self._level_delta_users = abs(new_users - prev)
        self._level_signed_delta = new_users - prev
        self._step = self._level_delta_users
        self._last_ramp_step = self._step
        return self._level_delta_users

    def _recovery_begin_settle(self, t: float) -> None:
        self._recovery_settle_start_t = float(t)
        self._window_start_reqs = None
        self._window_start_fails = None
        self._sample_ticks = []
        self._goodput_samples = []
        self._lat_samples = []

    def _measure_start_s(self) -> float:
        if self._phase == "explore":
            return float(self._p.spawn_settle_buffer_s) + float(self._level_drain_s)
        if self._phase == "refine":
            return (
                self._spawn_settle_s()
                + float(self._refine_measure_window_s)
                + float(self._level_drain_s)
            )
        if self._phase == "recovery":
            return float(self._p.spawn_settle_buffer_s) + float(self._level_drain_s)
        return super()._measure_start_s()

    def _spawn_settle_s(self) -> float:
        if self._phase in ("explore", "recovery"):
            return 0.0
        return super()._spawn_settle_s()

    def _spawn_complete(self, t: float) -> bool:
        if self._phase == "explore":
            return self._ramp_spawn_caught_up()
        if self._phase == "recovery":
            return True
        return super()._spawn_complete(t)

    def _spawn_rate(self) -> int:
        if self._phase == "explore":
            delta = int(getattr(self, "_level_delta_users", 0))
            if delta > 0:
                interval = self._explore_ramp_interval_from_p95(
                    self._current_explore_p95_ms()
                )
                rate_for_step = max(1, int(math.ceil(delta / interval)))
                return max(1, min(int(self._users), rate_for_step))
        return super()._spawn_rate()

    def _explore_bump_users(self, t: float) -> int:
        return self._sync_explore_users(t)

    def _rolling_decision_metrics(self) -> tuple[float, float, float | None, bool]:
        """(goodput_rps, fail_pct, p95_ms, have_enough) from trailing window."""
        n = self._decision_window_size()
        recent_lat = self._lat_samples[-n:]
        recent_ticks = self._sample_ticks[-(n + 1) :]
        have_enough = len(recent_lat) >= n and len(recent_ticks) >= n + 1
        recent_goodput = self._goodput_samples[-n:]
        goodput_rps = (
            float(statistics.mean(recent_goodput))
            if recent_goodput
            else float(self._level_goodput_from_window())
        )
        win_reqs, win_fails = self._level_window_counts()
        fail_pct = (100.0 * win_fails / win_reqs) if win_reqs > 0 else 0.0
        p95_ms = max(recent_lat) if recent_lat else None
        return goodput_rps, fail_pct, p95_ms, have_enough

    def _update_explore_peak(self, goodput_rps: float) -> None:
        gp = float(goodput_rps)
        if gp > float(self._explore_peak_goodput):
            self._explore_peak_goodput = gp
            self._explore_peak_users = int(self._users)
            self._explore_collapse_streak = 0

    def _explore_stop_reason(
        self,
        *,
        goodput_rps: float,
        fail_pct: float,
        p95_ms: float | None,
    ) -> str | None:
        thr = float(self._p.failure_threshold_pct)
        if fail_pct > thr:
            return f"fail%={fail_pct:.1f}>thr={thr:.1f}"
        if p95_ms is not None and p95_ms > float(self._p.overload_p95_ms):
            return f"p95={p95_ms:.0f}>{float(self._p.overload_p95_ms):.0f}ms"
        peak = float(self._explore_peak_goodput)
        if peak > 0 and goodput_rps < self._explore_stop_ratio * peak:
            self._explore_collapse_streak += 1
            n = int(self._explore_stop_steps)
            if self._explore_collapse_streak >= n:
                return (
                    f"goodput={goodput_rps:.1f}<{self._explore_stop_ratio:.0%}*peak="
                    f"{self._explore_stop_ratio * peak:.1f} ({self._explore_collapse_streak} checks)"
                )
        else:
            self._explore_collapse_streak = 0
        return None

    def _begin_recovery(self, reason: str, *, t: float | None = None) -> str:
        self._phase = "recovery"
        peak_u = int(self._explore_peak_users or self._users)
        peak_g = float(self._explore_peak_goodput)
        self._high_bad = int(self._users)
        self._recovery_peak_users = peak_u
        self._recovery_attempt = 1
        self._recovery_settle_start_t = 0.0
        floor_users = self._recovery_initial_floor_users(peak_u)
        drop = self._recovery_set_user_level(floor_users)
        if t is not None:
            self._recovery_begin_settle(t)
        return (
            f"explore end ({reason}) peak={peak_u}u/{peak_g:.1f}/s "
            f"-> recovery settle attempt={self._recovery_attempt} "
            f"users={self._users} ({self._recovery_floor_fraction:.0%} peak, "
            f"drop={drop}, hold={self._recovery_settle_duration_s}s)"
        )

    def _recovery_is_healthy(
        self,
        *,
        fail_pct: float,
        p95_ms: float | None,
        stable: bool,
    ) -> bool:
        if not stable:
            return False
        if p95_ms is not None and p95_ms > float(self._p.overload_p95_ms):
            return False
        if float(fail_pct) > float(self._p.failure_threshold_pct):
            return False
        return True

    def _recovery_retry_or_refine(
        self,
        t: float,
        *,
        goodput_rps: float,
        fail_pct: float,
        p95_ms: float | None,
        p95_spread_pct: float,
    ) -> str | None:
        """Drop to a lower floor and re-settle, or force refine at the floor."""
        floor = int(self._p.start_users)
        prev_users = int(self._users)
        new_users = self._recovery_retry_users(prev_users)
        if new_users >= prev_users or prev_users <= floor:
            self._low_ok = prev_users
            action = self._begin_refine(t)
            return (
                f"recovery floor at users={prev_users} after "
                f"{self._recovery_attempt} attempt(s); {action}"
            )

        self._recovery_attempt += 1
        drop = self._recovery_set_user_level(new_users)
        self._recovery_begin_settle(t)
        self._enter_level(t)
        p95_part = f" p95={p95_ms:.0f}ms" if p95_ms is not None else ""
        return (
            f"recovery retry attempt={self._recovery_attempt} "
            f"-{drop} ({self._recovery_retry_drop_fraction:.0%}) "
            f"-> users={self._users} settle={self._recovery_settle_duration_s}s{p95_part}"
        )

    def _begin_refine(self, t: float) -> str:
        lower = max(int(self._p.start_users), int(self._users))
        self._low_ok = lower
        self._refine_max_step = max(
            int(self._refine_min_step_users),
            int(round(lower * self._refine_max_step_frac)),
        )
        self._refine_min_step = int(self._refine_min_step_users)
        self._refine_initial_step = max(
            int(self._refine_min_step),
            int(round(lower * self._refine_initial_step_frac)),
        )
        self._phase = "refine"
        self._users = lower
        self._step = 0
        self._last_ramp_step = 0
        self._goodput_history = []
        self._best_goodput = 0.0
        self._stall_count = 0
        self._backoff_streak = 0
        self._efficiency_reset = False
        self._refine_best_goodput = 0.0
        self._refine_stall_count = 0
        self._enter_level(t)
        _LOG.info(
            "explore-refine: refine start at users=%s init_step=%s max_step=%s "
            "min_step=%s explore_peak=%.1f/s measure_window_s=%.0f",
            lower,
            self._refine_initial_step,
            self._refine_max_step,
            self._refine_min_step,
            float(self._explore_peak_goodput),
            self._refine_measure_window_s,
        )
        return (
            f"refine at users={lower} init_step={self._refine_initial_step} "
            f"max_step={self._refine_max_step}"
        )

    def _check_stall(self, goodput_rps: float, *, stable: bool) -> str | None:
        if self._phase == "refine":
            return None
        return super()._check_stall(goodput_rps, stable=stable)

    def _check_refine_level_stall(self, users: int, goodput_rps: float) -> str | None:
        improved = float(goodput_rps) > float(self._refine_best_goodput)
        if improved:
            self._refine_best_goodput = float(goodput_rps)
            self._refine_stall_count = 0
            return None

        self._refine_stall_count += 1
        n = int(self._refine_stop_steps)
        if self._refine_stall_count < n:
            return None

        self._done = True
        self._stop_reason = "refine-goodput-stall"
        return (
            f"refine stall ({self._refine_stall_count} consecutive steps "
            f"without goodput improvement) stopping at users={users} "
            f"best_goodput={self._refine_best_goodput:.1f}/s"
        )

    def _decide_refine_stepping(
        self, *, fail_pct: float, goodput_rps: float, stable: bool, p95_ms: float | None
    ) -> str:
        """Efficiency-based ramp stepping for the refine phase."""
        collapse_thr = float(self._p.collapse_threshold_pct) / 100.0
        overloaded_reasons: list[str] = []
        thr = float(self._p.failure_threshold_pct)
        if float(fail_pct) > thr:
            overloaded_reasons.append(f"fail%={fail_pct:.1f}>thr={thr:.1f}")
        if p95_ms is not None and p95_ms > float(self._p.overload_p95_ms):
            overloaded_reasons.append(
                f"p95={p95_ms:.0f}>{float(self._p.overload_p95_ms):.0f}ms"
            )
        if (
            self._best_goodput > 0
            and goodput_rps < (1.0 - collapse_thr) * self._best_goodput
        ):
            overloaded_reasons.append(
                f"goodput={goodput_rps:.1f}<{(1.0 - collapse_thr) * self._best_goodput:.1f}"
            )

        if overloaded_reasons:
            reason = " ".join(overloaded_reasons)
            if (
                self._backoff_streak >= int(self._refine_overload_backoff_max)
                and self._best_goodput > 0
            ):
                self._done = True
                self._stop_reason = "overload-peak"
                return (
                    f"refine overload ({reason}) after {self._backoff_streak} backoffs; "
                    f"stopping at users={self._users} "
                    f"best_goodput={self._best_goodput:.1f}/s"
                )
            return self._backoff(reason=reason)

        self._backoff_streak = 0
        self._low_ok = max(self._low_ok or 0, int(self._users))

        if stable:
            self._record_stable_step(float(goodput_rps))

        if int(self._users) >= int(self._p.max_users):
            self._done = True
            self._stop_reason = "max-users-reached"
            return f"refine max users ceiling hit at users={self._users}; stopping"

        last_stable = self._goodput_history[-1] if self._goodput_history else None
        if last_stable is None:
            return (
                f"refine ramp wait stable fail%={fail_pct:.1f} -> users={self._users}"
            )

        if self._efficiency_reset:
            self._efficiency_reset = False
            self._apply_ramp_step(int(self._refine_min_step))
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            stab_note = "" if stable else " unstable-cap"
            return (
                f"refine ramp recovery{stab_note} -> users={self._users} "
                f"step={self._step}"
            )

        prev_users, prev_goodput = last_stable
        eff = self._goodput_efficiency(
            prev_users, prev_goodput, int(self._users), float(goodput_rps)
        )
        base_step = float(self._last_ramp_step or self._step or self._refine_min_step)
        eff_thr = float(self._refine_efficiency_good_threshold)
        if eff >= eff_thr:
            band = "high-eff"
            factor = float(self._refine_step_growth)
            uncapped = int(round(base_step * factor))
        else:
            band = "low-eff"
            e = max(0.0, min(1.0, float(eff)))
            factor = e * e * e
            if factor <= 0:
                return (
                    f"refine hold eff={eff:.2f} band={band} factor={factor:.3f} "
                    f"fail%={fail_pct:.1f} -> users={self._users} step=0"
                )
            uncapped = int(round(base_step * factor))
        if uncapped < int(self._refine_min_step):
            return (
                f"refine hold eff={eff:.2f} band={band} uncapped={uncapped}"
                f"<min={self._refine_min_step} fail%={fail_pct:.1f} -> "
                f"users={self._users} step=0"
            )
        self._apply_ramp_step(uncapped)
        self._users = min(int(self._p.max_users), int(self._users + self._step))
        stab_note = "" if stable else " unstable-cap"
        return (
            f"refine ramp eff={eff:.2f} band={band} factor={factor:.3f} "
            f"fail%={fail_pct:.1f}{stab_note} -> users={self._users} step={self._step}"
        )

    def _decide_refine(
        self, *, fail_pct: float, goodput_rps: float, stable: bool, p95_ms: float | None
    ) -> str:
        if not self._goodput_history:
            if stable:
                baseline_users = int(self._users)
                self._record_stable_step(float(goodput_rps))
                self._low_ok = max(self._low_ok or 0, baseline_users)
                self._refine_best_goodput = max(
                    float(self._refine_best_goodput), float(goodput_rps)
                )
                self._refine_stall_count = 0
                initial_step = int(
                    self._refine_initial_step or self._refine_min_step or self._p.min_step_users
                )
                self._apply_ramp_step(initial_step)
                self._users = min(int(self._p.max_users), baseline_users + int(self._step))
                return (
                    f"refine baseline at users={baseline_users} "
                    f"goodput={goodput_rps:.1f}/s -> ramp +{self._step} "
                    f"users={self._users}"
                )
            return f"refine baseline wait stable fail%={fail_pct:.1f} -> users={self._users}"

        stall_stop = self._check_refine_level_stall(int(self._users), float(goodput_rps))
        if stall_stop is not None:
            return stall_stop

        return self._decide_refine_stepping(
            fail_pct=fail_pct, goodput_rps=goodput_rps, stable=stable, p95_ms=p95_ms
        )

    def _decide_and_advance(
        self, *, fail_pct: float, goodput_rps: float, stable: bool, p95_ms: float | None
    ) -> str:
        if self._phase == "refine":
            return self._decide_refine(
                fail_pct=fail_pct, goodput_rps=goodput_rps, stable=stable, p95_ms=p95_ms
            )
        return super()._decide_and_advance(
            fail_pct=fail_pct, goodput_rps=goodput_rps, stable=stable, p95_ms=p95_ms
        )

    def _sample_tick(self, t: float) -> None:
        elapsed = t - self._level_start_t
        if elapsed < self._measure_start_s():
            return
        reqs_now, fails_now = self._totals()
        if self._window_start_reqs is None:
            self._window_start_reqs = reqs_now
            self._window_start_fails = fails_now
            self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
            v = self._read_latency_ms()
            if v is not None and v > 0:
                self._lat_samples.append(v)
            return

        self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
        v = self._read_latency_ms()
        if v is not None and v > 0:
            self._lat_samples.append(v)

        roll_goodput = self._level_goodput_from_window()
        rates = self._current_rates()
        if rates is not None:
            rps, failps = rates
            roll_goodput = max(0.0, rps - failps)
            win_fail_pct = (100.0 * failps / rps) if rps > 0 else 0.0
        else:
            win_reqs, win_fails = self._level_window_counts()
            win_fail_pct = (100.0 * win_fails / win_reqs) if win_reqs > 0 else 0.0
        self._goodput_samples.append(float(roll_goodput))
        if v is not None and v > 0:
            _LOG.info(
                "adaptive sample t=%.0fs users=%s goodput=%.1f/s fail_pct=%.2f%% p95=%.0fms",
                t,
                self._users,
                roll_goodput,
                win_fail_pct,
                v,
            )

    def _tick_explore(self):
        if self._should_stop():
            if self._stop_reason is None:
                self._stop_reason = "run-time-elapsed"
            self._emit_final()
            return None
        if self._done:
            self._emit_final()
            return None

        t = float(self.get_run_time())
        self._last_tick_t = t
        if self._level_start_t <= 0.0:
            self._enter_level(t)

        if self._should_abort_no_healthy_users(t):
            self._emit_final()
            return None

        elapsed = t - self._level_start_t
        if self._is_warmup:
            if elapsed >= float(self._explore_warmup_duration_s):
                _LOG.info(
                    "explore-refine warmup end t=%.0fs at users=%s | %s",
                    t,
                    self._users,
                    self._stats_snapshot(),
                )
                self._is_warmup = False
                self._level_start_t = t
                self._explore_last_bump_t = t
                self._window_start_reqs = None
                self._window_start_fails = None
                self._sample_ticks = []
                self._goodput_samples = []
                self._lat_samples = []
            return int(self._users), self._spawn_rate()

        if t >= self._next_sample_t:
            self._sample_tick(t)
            self._next_sample_t = t + float(self._p.sample_every_s)

        p95_for_interval = self._current_explore_p95_ms()
        ramp_interval_s = self._explore_ramp_interval_from_p95(p95_for_interval)
        since_bump = (
            t - float(self._explore_last_bump_t)
            if self._explore_last_bump_t > 0.0
            else ramp_interval_s
        )
        if self._ramp_spawn_caught_up() and since_bump >= ramp_interval_s:
            step = self._sync_explore_users(t)
            if step > 0:
                self._explore_last_bump_t = t
                self._enter_level(t)
                p95_label = (
                    f"{p95_for_interval:.0f}ms"
                    if p95_for_interval is not None
                    else "n/a"
                )
                _LOG.info(
                    "explore ramp t=%.0fs +%s (max(%s, %.1f%%×%s)) "
                    "interval=%.0fs p95=%s -> users=%s peak=%.1f/s",
                    t,
                    step,
                    self._explore_min_step_users,
                    100.0 * self._explore_ramp_fraction,
                    int(self._users) - step,
                    ramp_interval_s,
                    p95_label,
                    self._users,
                    float(self._explore_peak_goodput),
                )

        goodput_rps, fail_pct, p95_ms, have_enough = self._rolling_decision_metrics()
        if not have_enough:
            return int(self._users), self._spawn_rate()

        decision_interval = float(self._p.sample_every_s)
        if (t - self._explore_last_decision_t) < decision_interval:
            return int(self._users), self._spawn_rate()
        self._explore_last_decision_t = t

        self._update_explore_peak(goodput_rps)
        stop = self._explore_stop_reason(
            goodput_rps=goodput_rps, fail_pct=fail_pct, p95_ms=p95_ms
        )
        if stop is not None:
            action = self._begin_recovery(stop, t=t)
            _LOG.info(
                "adaptive phase end t=%.0fs: %s | %s | step_goodput=%.1f/s",
                t,
                action,
                self._stats_snapshot(),
                goodput_rps,
            )
            self._enter_level(t)
            return int(self._users), self._spawn_rate()

        if int(self._users) >= int(self._p.max_users):
            action = self._begin_recovery(f"max users={self._users} reached", t=t)
            _LOG.info(
                "adaptive phase end t=%.0fs: %s | %s | step_goodput=%.1f/s",
                t,
                action,
                self._stats_snapshot(),
                goodput_rps,
            )
            self._enter_level(t)

        return int(self._users), self._spawn_rate()

    def _tick_recovery(self):
        if self._should_stop():
            if self._stop_reason is None:
                self._stop_reason = "run-time-elapsed"
            self._emit_final()
            return None
        if self._done:
            self._emit_final()
            return None

        t = float(self.get_run_time())
        self._last_tick_t = t
        if self._level_start_t <= 0.0:
            self._enter_level(t)
        if self._recovery_settle_start_t <= 0.0:
            self._recovery_begin_settle(t)

        if self._should_abort_no_healthy_users(t):
            self._emit_final()
            return None

        if t >= self._next_sample_t:
            self._sample_tick(t)
            self._next_sample_t = t + float(self._p.sample_every_s)

        settle_elapsed = t - float(self._recovery_settle_start_t)
        if settle_elapsed < float(self._recovery_settle_duration_s):
            return int(self._users), self._spawn_rate()

        goodput_rps, fail_pct, p95_ms, have_enough = self._rolling_decision_metrics()
        if not have_enough:
            return int(self._users), self._spawn_rate()

        n = self._decision_window_size()
        recent_lat = self._lat_samples[-n:]
        p95_spread_pct = _sample_spread_pct(recent_lat)
        stable = p95_spread_pct <= float(self._p.stability_drift_threshold_pct)

        if self._recovery_is_healthy(
            fail_pct=float(fail_pct),
            p95_ms=p95_ms,
            stable=bool(stable),
        ):
            action = self._begin_refine(t)
            _LOG.info(
                "adaptive phase end t=%.0fs: recovery healthy -> %s | %s | "
                "step_goodput=%.1f/s p95=%s fail%%=%.1f p95_spread=%.1f%% "
                "attempt=%s settle=%.0fs",
                t,
                action,
                self._stats_snapshot(),
                goodput_rps,
                f"{p95_ms:.0f}ms" if p95_ms is not None else "n/a",
                fail_pct,
                p95_spread_pct,
                self._recovery_attempt,
                settle_elapsed,
            )
            return int(self._users), self._spawn_rate()

        action = self._recovery_retry_or_refine(
            t,
            goodput_rps=float(goodput_rps),
            fail_pct=float(fail_pct),
            p95_ms=p95_ms,
            p95_spread_pct=p95_spread_pct,
        )
        _LOG.info(
            "adaptive phase end t=%.0fs: recovery unhealthy -> %s | %s | "
            "step_goodput=%.1f/s p95=%s fail%%=%.1f p95_spread=%.1f%%",
            t,
            action,
            self._stats_snapshot(),
            goodput_rps,
            f"{p95_ms:.0f}ms" if p95_ms is not None else "n/a",
            fail_pct,
            p95_spread_pct,
        )
        return int(self._users), self._spawn_rate()

    def tick(self):
        self._last_tick_t = float(self.get_run_time())
        if self._phase == "explore":
            return self._tick_explore()
        if self._phase == "recovery":
            return self._tick_recovery()
        return super().tick()


def _selected_shape_class() -> type[_BaseShape]:
    mode = (_load_manifest().get("mode") or "steady").strip().lower()
    mapping: dict[str, type[_BaseShape]] = {
        "steady": SteadyShape,
        "continuous": ContinuousShape,
        "stairs": StairsShape,
        "spike": SpikeShape,
        "adaptive": AdaptiveShape,
        "adaptive_v2": AdaptiveV2Shape,
        "goodput_plateau": GoodputPlateauShape,
        "explore_refine": ExploreRefineShape,
    }
    return mapping.get(mode, SteadyShape)


# Backwards-compatible export used by scenario locustfiles:
# `from _baxbench_shape import BaxbenchShape`
BaxbenchShape = _selected_shape_class()


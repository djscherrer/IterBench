from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path

from locust import LoadTestShape, between

_LOAD_PROFILE_MANIFEST = "baxbench_load_profile.json"

_LOG = logging.getLogger("baxbench.adaptive")

_manifest: dict | None = None


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
    return (hi - lo) / mean * 100.0


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

    health_grace_s: int
    abort_on_no_users: bool


def _goodput_plateau_params_from_manifest() -> _GoodputPlateauParams:
    cfg = _load_manifest()
    trim_s = max(0, int(cfg["trim_s"]))
    min_step_s = max(5, int(cfg["min_step_duration_s"]))
    max_step_s = max(min_step_s, int(cfg["max_step_duration_s"]))
    return _GoodputPlateauParams(
        failure_threshold_pct=float(cfg["failure_threshold_pct"]),
        collapse_threshold_pct=float(cfg["collapse_threshold_pct"]),
        overload_p95_ms=float(cfg["overload_p95_ms"]),
        start_users=max(1, int(cfg["start_users"])),
        max_users=max(1, int(cfg["max_users"])),
        min_step_users=max(1, int(cfg["min_step_users"])),
        max_step_users=max(1, int(cfg["max_step_users"])),
        step_up_gain=max(1.0, float(cfg["step_up_gain"])),
        efficiency_good_threshold=max(
            0.0, min(1.0, float(cfg["efficiency_good_threshold"]))
        ),
        drain_time_s=max(0, int(cfg["drain_time_s"])),
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


class GoodputPlateauShape(_BaseShape):
    """
    Goodput plateau controller.

    Per level, after trim (+ optional drain):
    - Sample interval goodput and p95 every ``sample_every_s``.
    - Once ``min_settle_samples`` readings exist and p95 spread in that window is
      ≤ ``stability_drift_threshold_pct``, end the step (or at ``max_step_duration_s``).
    - ``step_goodput`` logged at phase end = mean of those window samples (not the
      full-step average including trim/spawn).
    - Ramps by marginal goodput efficiency between consecutive level goodputs.
    - Backs off on overload by subtracting the last ramp step, scaling the
      drop by ``step_up_gain`` on each consecutive overload.
    - After backoff, waits an extra ``drain_time_s`` before sampling/measuring
      so in-flight queue can drain.

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

        # Per-interval samples in the settle / decision window (after trim+drain).
        self._lat_samples: list[float] = []
        self._goodput_samples: list[float] = []

        self._level_start_reqs = 0
        self._level_start_fails = 0
        self._last_sample_reqs = 0
        self._last_sample_fails = 0
        self._last_sample_t: float | None = None
        self._window_start_reqs: int | None = None
        self._window_start_fails: int | None = None
        self._sample_ticks: list[tuple[float, int, int]] = []
        self._is_warmup = self._p.warmup_step_duration_s > 0

        self._goodput_history: list[tuple[int, float]] = []  # (users, level goodput rps)
        self._best_goodput: float = 0.0

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
            "stability_drift_pct=%s plateau_stop_steps=%s plateau_pct=%s "
            "step_up_gain=%s eff_good_thr=%s drain_s=%s max_users=%s",
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
            self._p.plateau_goodput_threshold_pct,
            self._p.step_up_gain,
            self._p.efficiency_good_threshold,
            self._p.drain_time_s,
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
        self._level_start_t = t
        self._next_sample_t = t
        self._level_drain_s = int(self._pending_drain_s)
        self._pending_drain_s = 0
        self._lat_samples = []
        self._goodput_samples = []
        reqs, fails = self._totals()
        self._level_start_reqs = reqs
        self._level_start_fails = fails
        self._last_sample_reqs = reqs
        self._last_sample_fails = fails
        self._last_sample_t = None
        self._window_start_reqs = None
        self._window_start_fails = None
        self._sample_ticks = []
        self._abort_logged = False

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

    def _measure_start_s(self) -> float:
        return float(self._p.trim_s) + float(self._level_drain_s)

    def _min_level_duration_s(self) -> float:
        return float(self._p.min_step_duration_s) + float(self._level_drain_s)

    def _max_level_duration_s(self) -> float:
        return float(self._p.max_step_duration_s) + float(self._level_drain_s)

    def _spawn_rate(self) -> int:
        return max(1, min(int(self._p.spawn_rate), int(self._step), int(self._users)))

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

    @staticmethod
    def _goodput_efficiency(prev_users: int, prev_goodput: float, users: int, goodput: float) -> float:
        if prev_users <= 0 or prev_goodput <= 0:
            return 0.0
        if users <= prev_users:
            return 0.0
        d_goodput_frac = (goodput - prev_goodput) / prev_goodput
        d_users_frac = (users - prev_users) / prev_users
        if d_users_frac <= 0:
            return 0.0
        return d_goodput_frac / d_users_frac

    @staticmethod
    def _ramp_factor_from_efficiency(eff: float) -> tuple[float, str]:
        """
        Convert marginal goodput efficiency into a step fraction.

        We cube efficiency so the controller becomes more conservative near the
        plateau: e.g. 0.8 -> 0.512, 0.7 -> 0.343, 0.5 -> 0.125.
        """
        e = max(0.0, min(1.0, float(eff)))
        factor = e * e * e
        if factor <= 0:
            return 0.0, "near-plateau"
        if factor >= 0.75:
            return factor, "high-eff"
        if factor >= 0.25:
            return factor, "good-eff"
        if factor >= 0.10:
            return factor, "mid-eff"
        if factor >= 0.03:
            return factor, "low-eff"
        return factor, "very-low-eff"

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
        if self._best_goodput > 0 and goodput_rps < (1.0 - collapse_thr) * self._best_goodput:
            overloaded_reasons.append(
                f"goodput={goodput_rps:.1f}<{(1.0 - collapse_thr) * self._best_goodput:.1f}"
            )

        if overloaded_reasons:
            return self._backoff(reason=" ".join(overloaded_reasons))

        # Passing step.
        self._backoff_streak = 0
        self._low_ok = max(self._low_ok or 0, int(self._users))
        self._goodput_history.append((int(self._users), float(goodput_rps)))
        self._best_goodput = max(self._best_goodput, float(goodput_rps))
        if self._check_plateau():
            self._done = True
            self._stop_reason = "goodput-plateau"
            return (
                f"plateau (last {self._p.plateau_stop_steps} steps grew goodput "
                f"< {self._p.plateau_goodput_threshold_pct:g}%) stopping at users={self._users}"
            )

        # Bracket refinement if we have both endpoints.
        if self._low_ok is not None and self._high_bad is not None and self._high_bad > self._low_ok:
            gap = int(self._high_bad - self._low_ok)
            if gap <= int(self._p.min_step_users):
                self._done = True
                self._stop_reason = "bracket-narrow"
                return f"bracket narrow (low={self._low_ok} high={self._high_bad}); stopping at users={self._users}"
            self._apply_ramp_step(int(gap // 2))
            self._users = min(int(self._p.max_users), int(self._low_ok + self._step))
            return f"bracket refine low={self._low_ok} high={self._high_bad} -> users={self._users} step={self._step}"

        # Explore: ramp up based on marginal goodput efficiency.
        if int(self._users) >= int(self._p.max_users):
            self._done = True
            self._stop_reason = "max-users-reached"
            return f"max users ceiling hit at users={self._users}; stopping"

        if len(self._goodput_history) < 2:
            # First passing step after warmup: take a large initial step to reach
            # the interesting region quickly; growth cap applies from step 2 onward.
            self._apply_ramp_step(int(self._p.max_step_users))
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            stab_note = "" if stable else " unstable-cap"
            return f"goodput ramp init{stab_note} -> users={self._users} step={self._step}"

        prev_users, prev_goodput = self._goodput_history[-2]
        eff = self._goodput_efficiency(prev_users, prev_goodput, int(self._users), float(goodput_rps))
        if self._efficiency_reset:
            self._efficiency_reset = False
            self._step = int(self._p.min_step_users)
            self._last_ramp_step = int(self._step)
            self._users = min(int(self._p.max_users), int(self._users + self._step))
            stab_note = "" if stable else " unstable-cap"
            return f"goodput ramp reset{stab_note} -> users={self._users} step={self._step}"

        if eff < 0:
            # Negative marginal returns without a collapse/latency trigger: undo the last step,
            # reset to min-step exploration, and re-measure from the last known-good point.
            self._high_bad = (
                int(self._users)
                if self._high_bad is None
                else min(self._high_bad, int(self._users))
            )
            self._users = int(prev_users)
            self._step = int(self._p.min_step_users)
            self._last_ramp_step = int(self._step)
            self._efficiency_reset = True
            return (
                f"goodput negative-eff eff={eff:.2f} undo -> users={self._users} step={self._step}"
            )

        # Step-relative ramp: grow with step_up_gain when efficient, else shrink with eff³.
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

        # Sample p95 and per-interval goodput after trim (+ drain), not during spawn-in.
        if t >= self._next_sample_t:
            elapsed = t - self._level_start_t
            if elapsed >= self._measure_start_s():
                reqs_now, fails_now = self._totals()
                if self._window_start_reqs is None:
                    self._window_start_reqs = reqs_now
                    self._window_start_fails = fails_now
                    self._last_sample_reqs = reqs_now
                    self._last_sample_fails = fails_now
                    self._last_sample_t = float(t)
                    self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
                    v = self._read_latency_ms()
                    if v is not None and v > 0:
                        self._lat_samples.append(v)
                        _LOG.info(
                            "adaptive p95 sample t=%.0fs users=%s p95=%.0fms", t, self._users, v
                        )
                else:
                    v = self._read_latency_ms()
                    if v is not None and v > 0:
                        self._lat_samples.append(v)
                        _LOG.info(
                            "adaptive p95 sample t=%.0fs users=%s p95=%.0fms", t, self._users, v
                        )

                    if len(self._sample_ticks) >= 1:
                        t_prev, r_prev, f_prev = self._sample_ticks[-1]
                        dt = max(0.001, float(t) - float(t_prev))
                        succ_prev = max(0, int(r_prev) - int(f_prev))
                        succ_now = max(0, int(reqs_now) - int(fails_now))
                        interval_goodput = max(0.0, float(succ_now - succ_prev) / dt)
                        self._goodput_samples.append(float(interval_goodput))
                        _LOG.info(
                            "adaptive goodput sample t=%.0fs users=%s goodput=%.1f/s",
                            t,
                            self._users,
                            interval_goodput,
                        )
                    self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
                    self._last_sample_reqs = reqs_now
                    self._last_sample_fails = fails_now
                    self._last_sample_t = float(t)

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

        level_goodput_rps = self._level_goodput_from_window()
        recent_goodput = self._goodput_samples[-n:]
        reqs_now, fails_now = self._totals()
        win_reqs = max(0, reqs_now - int(self._window_start_reqs or reqs_now))
        win_fails = max(0, fails_now - int(self._window_start_fails or fails_now))
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
    }
    return mapping.get(mode, SteadyShape)


# Backwards-compatible export used by scenario locustfiles:
# `from _baxbench_shape import BaxbenchShape`
BaxbenchShape = _selected_shape_class()


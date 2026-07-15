from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from .base import BaseShape
from .manifest import _load_manifest
from .metrics import _latency_drift_pct

_LOG = logging.getLogger("baxbench.adaptive")

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


class AdaptiveV2Shape(BaseShape):
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

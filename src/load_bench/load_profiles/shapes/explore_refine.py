from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass

from .base import BaseShape
from .manifest import (
    _load_manifest,
    _manifest_required,
    _validate_explore_refine_manifest,
)
from .metrics import (
    _max_deviation_from_mean_pct,
    active_user_count,
    append_latency_sample,
    current_rates,
    goodput_efficiency,
    log_sample,
    ramp_spawn_caught_up,
    read_latency_ms,
    rolling_goodput_and_fail_pct,
    stats_snapshot,
    totals,
)

_LOG = logging.getLogger("baxbench.adaptive")


@dataclass(frozen=True)
class _ExploreRefineParams:
    failure_threshold_pct: float
    overload_p95_ms: float
    start_users: int
    max_users: int
    sample_every_s: int
    quantile: float

    explore_warmup_duration_s: int
    explore_ramp_fraction: float
    explore_min_step_users: int
    explore_stop_ratio: float
    explore_stop_steps: int

    recovery_floor_fraction: float
    recovery_settle_duration_s: int
    recovery_retry_drop_fraction: float
    recovery_max_retries: int

    refine_max_step_duration_s: int
    refine_min_settle_samples: int
    refine_trim_s: float
    refine_min_step_users: int
    refine_max_step_fraction: float
    refine_goodput_stability_pct: float


def _explore_refine_params_from_manifest() -> _ExploreRefineParams:
    cfg = _load_manifest()
    _validate_explore_refine_manifest(cfg)
    return _ExploreRefineParams(
        failure_threshold_pct=float(_manifest_required(cfg, "failure_threshold_pct")),
        overload_p95_ms=float(_manifest_required(cfg, "overload_p95_ms")),
        start_users=max(1, int(_manifest_required(cfg, "start_users"))),
        max_users=max(1, int(_manifest_required(cfg, "max_users"))),
        sample_every_s=max(1, int(_manifest_required(cfg, "sample_every_s"))),
        quantile=float(_manifest_required(cfg, "quantile")),
        explore_warmup_duration_s=max(
            0, int(_manifest_required(cfg, "explore_warmup_duration_s"))
        ),
        explore_ramp_fraction=max(
            0.001,
            min(
                1.0,
                float(_manifest_required(cfg, "explore_ramp_user_fraction_per_s")),
            ),
        ),
        explore_min_step_users=max(
            1, int(_manifest_required(cfg, "explore_min_step_users"))
        ),
        explore_stop_ratio=max(
            0.5,
            min(1.0, float(_manifest_required(cfg, "explore_goodput_stop_ratio"))),
        ),
        explore_stop_steps=max(1, int(_manifest_required(cfg, "explore_stop_steps"))),
        recovery_floor_fraction=max(
            0.1,
            min(1.0, float(_manifest_required(cfg, "recovery_floor_fraction"))),
        ),
        recovery_settle_duration_s=max(
            5, int(_manifest_required(cfg, "recovery_settle_duration_s"))
        ),
        recovery_retry_drop_fraction=max(
            0.01,
            min(1.0, float(_manifest_required(cfg, "recovery_retry_drop_fraction"))),
        ),
        recovery_max_retries=max(
            1, int(_manifest_required(cfg, "recovery_max_retries"))
        ),
        refine_max_step_duration_s=max(
            5, int(_manifest_required(cfg, "refine_max_step_duration_s"))
        ),
        refine_min_settle_samples=max(
            2, int(_manifest_required(cfg, "refine_min_settle_samples"))
        ),
        refine_trim_s=max(1.0, float(_manifest_required(cfg, "refine_trim_s"))),
        refine_min_step_users=max(
            1, int(_manifest_required(cfg, "refine_min_step_users"))
        ),
        refine_max_step_fraction=float(
            _manifest_required(cfg, "refine_max_step_fraction")
        ),
        refine_goodput_stability_pct=max(
            0.0, float(_manifest_required(cfg, "refine_goodput_stability_pct"))
        ),
    )


class ExploreRefineShape(BaseShape):
    """
    Three-phase goodput finder: explore ramp, settle-floor recovery, fine refine.

    Explore bumps users by ``explore_ramp_user_fraction_per_s`` on a P95-adaptive
    interval: ``max(1s, ceil(p95_ms / 50ms))``. Recovery jumps to
    ``recovery_floor_fraction`` × explore peak users, holds for
    ``recovery_settle_duration_s``, then checks fail% and absolute p95. On failure it
    drops by ``recovery_retry_drop_fraction`` and settles again before refine.
    After ``recovery_max_retries`` unhealthy settle attempts (including the
    initial floor settle), recovery stops without entering refine.

    Refine settles a user level when the last ``refine_min_settle_samples`` rolling
    goodput samples each lie within ``refine_goodput_stability_pct`` of their mean.
    Improving levels are recorded; next bump is ``efficiency × max_step`` where
    ``max_step`` is ``refine_max_step_fraction`` of recovery-entry users. If a later
    settled level does not beat the best refine goodput, stop immediately and keep
    that peak. Fail% above threshold also stops immediately. No user backoff.
    """

    def __init__(self):
        super().__init__()
        self._p = _explore_refine_params_from_manifest()
        self._users = int(self._p.start_users)
        self._step = 0
        self._last_ramp_step = 0
        self._pending_drain_s = 0
        self._level_drain_s = 0
        self._level_start_t = 0.0
        self._next_sample_t = 0.0
        self._level_delta_users = 0
        self._level_signed_delta = 0
        self._prev_level_users = int(self._users)

        self._lat_samples: list[float] = []
        self._goodput_samples: list[float] = []
        self._window_start_reqs: int | None = None
        self._window_start_fails: int | None = None
        self._sample_ticks: list[tuple[float, int, int]] = []

        self._is_warmup = self._p.explore_warmup_duration_s > 0
        self._phase = "explore"
        self._goodput_history: list[tuple[int, float]] = []
        self._refine_max_step: int | None = None
        self._refine_min_step: int | None = None
        self._refine_best_goodput: float = 0.0
        self._refine_best_users: int | None = None
        self._explore_peak_goodput: float = 0.0
        self._explore_peak_users: int | None = None
        self._explore_collapse_streak = 0
        self._explore_last_decision_t = 0.0
        self._explore_last_bump_t = 0.0
        self._recovery_peak_users: int | None = None
        self._recovery_settle_start_t = 0.0
        self._recovery_attempt = 0
        self._last_tick_t = 0.0

        self._low_ok: int | None = None
        self._high_bad: int | None = None
        self._done = False
        self._stop_reason: str | None = None
        self._final_logged = False
        self._window_unavailable_logged = False

        _LOG.info(
            "explore-refine init: warmup=%ss ramp_frac=%.1f%%/step ramp_interval=p95/50ms "
            "explore_min_step=%s explore_stop=%.0f%% explore_checks=%s "
            "recovery_floor=%.0f%% recovery_settle=%ss recovery_retry_drop=%.0f%% "
            "recovery_max_retries=%s refine_max_step=%.0f%% refine_trim_s=%.0f "
            "refine_settle=%s refine_max_step_s=%s",
            self._p.explore_warmup_duration_s,
            100.0 * self._p.explore_ramp_fraction,
            self._p.explore_min_step_users,
            100.0 * self._p.explore_stop_ratio,
            self._p.explore_stop_steps,
            100.0 * self._p.recovery_floor_fraction,
            self._p.recovery_settle_duration_s,
            100.0 * self._p.recovery_retry_drop_fraction,
            self._p.recovery_max_retries,
            100.0 * self._p.refine_max_step_fraction,
            self._p.refine_trim_s,
            self._p.refine_min_settle_samples,
            self._p.refine_max_step_duration_s,
        )

    def _totals(self) -> tuple[int, int]:
        return totals(self)

    def _rolling_goodput_and_fail_pct(self) -> tuple[float, float] | None:
        return rolling_goodput_and_fail_pct(self)

    def _read_latency_ms(self) -> float | None:
        return read_latency_ms(self, self._p.quantile)

    def _stats_snapshot(self) -> str:
        return stats_snapshot(self, self._p.quantile)

    def _active_user_count(self) -> int | None:
        return active_user_count(self)

    def _emit_final(self) -> None:
        if self._final_logged or self._stop_reason is None:
            return
        self._final_logged = True
        history = (
            ", ".join(f"{u}u:{g:.1f}/s" for u, g in self._goodput_history[-8:])
            or "(none)"
        )
        peak = ""
        if self._refine_best_goodput > 0:
            peak = f" refine_peak={self._refine_best_goodput:.1f}/s"
            if self._refine_best_users is not None:
                peak += f"@{self._refine_best_users}u"
        _LOG.info(
            "adaptive-v2 stop: reason=%s final_users=%s low_ok=%s high_bad=%s "
            "goodput_history=[%s]%s",
            self._stop_reason,
            self._users,
            self._low_ok,
            self._high_bad,
            history,
            peak,
        )

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
        self._window_start_reqs = None
        self._window_start_fails = None
        self._sample_ticks = []
        if self._level_delta_users > 0 and self._phase == "refine":
            _LOG.info(
                "adaptive level ramp +%s -> users=%s spawn_rate=%s "
                "spawn_s=%.1f trim_s=%.0f measure_start_s=%.1f",
                self._level_delta_users,
                self._users,
                self._spawn_rate(),
                self._refine_spawn_s(),
                self._p.refine_trim_s,
                self._measure_start_s(),
            )

    def _refine_spawn_s(self) -> float:
        """Fixed 1s arrival window for refine bumps (spawn rate = delta)."""
        if int(getattr(self, "_level_delta_users", 0)) <= 0:
            return 0.0
        return 1.0

    def _spawn_settle_s(self) -> float:
        if self._phase == "refine":
            return self._refine_spawn_s()
        return 0.0

    def _spawn_rate(self) -> int:
        if self._phase == "explore":
            delta = int(getattr(self, "_level_delta_users", 0))
            if delta > 0:
                interval = self._explore_ramp_interval_from_p95(
                    self._current_explore_p95_ms()
                )
                rate_for_step = max(1, int(math.ceil(delta / interval)))
                return max(1, min(int(self._users), rate_for_step))
            return max(1, int(self._users))
        # Refine/recovery: land the bump in ~1s.
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta > 0:
            return max(1, min(int(self._users), int(delta)))
        return max(1, int(self._users))

    def _spawn_complete(self, t: float) -> bool:
        if self._phase == "explore":
            return self._ramp_spawn_caught_up()
        if self._phase == "recovery":
            return True
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta <= 0:
            return True
        elapsed = t - self._level_start_t
        if elapsed < self._refine_spawn_s():
            return False
        return ramp_spawn_caught_up(
            active=self._active_user_count(),
            target_users=int(self._users),
            delta_users=delta,
            tolerance_floor=2,
        )

    def _ramp_spawn_caught_up(self) -> bool:
        return ramp_spawn_caught_up(
            active=self._active_user_count(),
            target_users=int(self._users),
            delta_users=int(getattr(self, "_level_delta_users", 0)),
            tolerance_floor=5,
        )

    def _measure_start_s(self) -> float:
        if self._phase == "refine":
            # 1s to arrive at the new user level, then refine_trim_s cool-in,
            # then collect settle samples for the next decision.
            return (
                self._refine_spawn_s()
                + float(self._p.refine_trim_s)
                + float(self._level_drain_s)
            )
        # Explore/recovery: sample as soon as the level is active.
        return float(self._level_drain_s)

    def _min_level_duration_s(self) -> float:
        # Min hold is spawn/trim + settle-window length (no separate min duration knob).
        measure_s = self._measure_start_s() - float(self._level_drain_s)
        window_s = float(self._p.refine_min_settle_samples) * float(
            self._p.sample_every_s
        )
        return measure_s + window_s + float(self._level_drain_s)

    def _max_level_duration_s(self) -> float:
        measure_s = self._measure_start_s() - float(self._level_drain_s)
        window_s = float(self._p.refine_min_settle_samples) * float(
            self._p.sample_every_s
        )
        needed = measure_s + window_s + 5.0
        return max(float(self._p.refine_max_step_duration_s), needed) + float(
            self._level_drain_s
        )

    def _decision_window_size(self) -> int:
        if self._phase in ("refine", "recovery"):
            return int(self._p.refine_min_settle_samples)
        return max(2, min(5, int(self._p.refine_min_settle_samples)))

    def _clamp_step_users(self, step_users: int) -> int:
        lo = int(self._refine_min_step or self._p.refine_min_step_users)
        hi = int(self._refine_max_step or lo)
        return max(lo, min(hi, int(step_users)))

    def _apply_ramp_step(self, raw_step: int) -> int:
        step_users = self._clamp_step_users(raw_step)
        self._step = step_users
        self._last_ramp_step = step_users
        return step_users

    def _record_level_goodput(self, goodput_rps: float) -> None:
        self._goodput_history.append((int(self._users), float(goodput_rps)))

    def _warmup_health_ok(self) -> tuple[bool, str]:
        active = self._active_user_count()
        if active is not None and int(active) <= 0:
            return False, "zero active users"
        rolled = self._rolling_goodput_and_fail_pct()
        if rolled is None:
            return False, "rolling rates unavailable"
        goodput_rps, fail_pct = rolled
        rates = current_rates(self) or (0.0, 0.0)
        if float(rates[0]) <= 0.0:
            return False, "no requests in rolling window"
        thr = float(self._p.failure_threshold_pct)
        if fail_pct > thr:
            return False, f"fail%={fail_pct:.1f}>thr={thr:.1f}"
        return (
            True,
            f"ok users={active if active is not None else '?'} "
            f"fail%={fail_pct:.1f} goodput={goodput_rps:.1f}/s",
        )

    def _explore_ramp_interval_from_p95(self, p95_ms: float | None) -> float:
        if p95_ms is None or float(p95_ms) <= 0.0:
            return 1.0
        return float(max(1, int(math.ceil(float(p95_ms) / 50.0))))

    def _current_explore_p95_ms(self) -> float | None:
        if self._lat_samples:
            return float(self._lat_samples[-1])
        return None

    def _explore_step_users(self, current_users: int) -> int:
        return max(
            int(self._p.explore_min_step_users),
            int(round(int(current_users) * float(self._p.explore_ramp_fraction))),
        )

    def _sync_explore_users(self) -> int:
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
        floor = int(round(int(peak_users) * float(self._p.recovery_floor_fraction)))
        return max(int(self._p.start_users), floor)

    def _recovery_retry_users(self, current_users: int) -> int:
        drop_frac = float(self._p.recovery_retry_drop_fraction)
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

    def _rolling_decision_metrics(self) -> tuple[float, float, float | None, bool]:
        """(goodput_rps, fail_pct, p95_ms, have_enough) for explore/recovery."""
        n = self._decision_window_size()
        recent_lat = self._lat_samples[-n:]
        recent_ticks = self._sample_ticks[-(n + 1) :]
        have_enough = len(recent_lat) >= n and len(recent_ticks) >= n + 1
        rolled = self._rolling_goodput_and_fail_pct()
        if rolled is not None:
            goodput_rps, fail_pct = rolled
        elif self._goodput_samples:
            goodput_rps = float(self._goodput_samples[-1])
            fail_pct = 0.0
        else:
            goodput_rps = 0.0
            fail_pct = 0.0
        if self._phase == "explore" and recent_lat:
            p95_ms = float(recent_lat[-1])
        else:
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
        if peak > 0 and goodput_rps < self._p.explore_stop_ratio * peak:
            self._explore_collapse_streak += 1
            if self._explore_collapse_streak >= int(self._p.explore_stop_steps):
                return (
                    f"goodput={goodput_rps:.1f}<{self._p.explore_stop_ratio:.0%}*peak="
                    f"{self._p.explore_stop_ratio * peak:.1f} "
                    f"({self._explore_collapse_streak} checks)"
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
            f"users={self._users} ({self._p.recovery_floor_fraction:.0%} peak, "
            f"drop={drop}, hold={self._p.recovery_settle_duration_s}s)"
        )

    def _recovery_is_healthy(
        self,
        *,
        fail_pct: float,
        p95_ms: float | None,
    ) -> bool:
        if p95_ms is not None and p95_ms > float(self._p.overload_p95_ms):
            return False
        if float(fail_pct) > float(self._p.failure_threshold_pct):
            return False
        return True

    def _recovery_abort_unhealthy(
        self,
        *,
        reason: str,
        fail_pct: float,
        p95_ms: float | None,
    ) -> str:
        self._done = True
        self._stop_reason = "recovery-unhealthy"
        p95_part = f" p95={p95_ms:.0f}ms" if p95_ms is not None else " p95=n/a"
        return (
            f"recovery gave up ({reason}) after {self._recovery_attempt} "
            f"settle attempt(s); refine not reached at users={self._users} "
            f"fail%={fail_pct:.1f}{p95_part}"
        )

    def _recovery_retry_or_refine(
        self,
        t: float,
        *,
        fail_pct: float,
        p95_ms: float | None,
    ) -> str:
        if int(self._recovery_attempt) >= int(self._p.recovery_max_retries):
            return self._recovery_abort_unhealthy(
                reason=f"max_retries={self._p.recovery_max_retries} unhealthy settles",
                fail_pct=float(fail_pct),
                p95_ms=p95_ms,
            )

        floor = int(self._p.start_users)
        prev_users = int(self._users)
        new_users = self._recovery_retry_users(prev_users)
        if new_users >= prev_users or prev_users <= floor:
            return self._recovery_abort_unhealthy(
                reason=f"floor users={prev_users} still unhealthy",
                fail_pct=float(fail_pct),
                p95_ms=p95_ms,
            )

        self._recovery_attempt += 1
        drop = self._recovery_set_user_level(new_users)
        self._recovery_begin_settle(t)
        self._enter_level(t)
        p95_part = f" p95={p95_ms:.0f}ms" if p95_ms is not None else ""
        return (
            f"recovery retry attempt={self._recovery_attempt}/"
            f"{self._p.recovery_max_retries} "
            f"-{drop} ({self._p.recovery_retry_drop_fraction:.0%}) "
            f"-> users={self._users} settle={self._p.recovery_settle_duration_s}s"
            f"{p95_part}"
        )

    def _begin_refine(self, t: float) -> str:
        lower = max(int(self._p.start_users), int(self._users))
        self._low_ok = lower
        self._refine_max_step = max(
            int(self._p.refine_min_step_users),
            int(round(lower * self._p.refine_max_step_fraction)),
        )
        self._refine_min_step = int(self._p.refine_min_step_users)
        self._phase = "refine"
        self._users = lower
        self._step = 0
        self._last_ramp_step = 0
        self._goodput_history = []
        self._refine_best_goodput = 0.0
        self._refine_best_users = None
        self._enter_level(t)
        _LOG.info(
            "explore-refine: refine start at users=%s max_step=%s "
            "(%.0f%% of entry) min_step=%s explore_peak=%.1f/s refine_trim_s=%.0f",
            lower,
            self._refine_max_step,
            100.0 * self._p.refine_max_step_fraction,
            self._refine_min_step,
            float(self._explore_peak_goodput),
            self._p.refine_trim_s,
        )
        return f"refine at users={lower} max_step={self._refine_max_step}"

    def _refine_fail_stop(self, *, fail_pct: float) -> str | None:
        thr = float(self._p.failure_threshold_pct)
        if float(fail_pct) <= thr:
            return None
        self._done = True
        self._stop_reason = "overload-peak"
        peak_u = self._refine_best_users
        peak_g = float(self._refine_best_goodput)
        peak_bit = ""
        if peak_g > 0:
            peak_bit = f" peak_goodput={peak_g:.1f}/s"
            if peak_u is not None:
                peak_bit += f"@{peak_u}u"
        return (
            f"refine overload (fail%={fail_pct:.1f}>thr={thr:.1f}); "
            f"stopping at users={self._users}{peak_bit}"
        )

    def _refine_next_step_users(self, *, goodput_rps: float) -> tuple[int, float]:
        max_step = int(self._refine_max_step or self._p.refine_min_step_users)
        min_step = int(self._refine_min_step or self._p.refine_min_step_users)
        if len(self._goodput_history) < 2:
            return max_step, 1.0
        prev_users, prev_goodput = self._goodput_history[-2]
        eff = goodput_efficiency(
            int(prev_users),
            float(prev_goodput),
            int(self._users),
            float(goodput_rps),
        )
        eff_for_step = max(0.0, min(1.0, float(eff)))
        step = int(round(eff_for_step * float(max_step)))
        if step > 0 and step < min_step:
            step = min_step
        step = min(step, max_step)
        return step, float(eff)

    def _decide_refine(
        self, *, fail_pct: float, goodput_rps: float, stable: bool
    ) -> str:
        fail_stop = self._refine_fail_stop(fail_pct=fail_pct)
        if fail_stop is not None:
            return fail_stop

        if not stable:
            return (
                f"refine wait goodput-stable fail%={fail_pct:.1f} "
                f"-> users={self._users}"
            )

        improved = float(goodput_rps) > float(self._refine_best_goodput)
        if not improved and float(self._refine_best_goodput) > 0:
            self._done = True
            self._stop_reason = "refine-goodput-stall"
            peak_u = self._refine_best_users
            peak_g = float(self._refine_best_goodput)
            peak_bit = f"peak_goodput={peak_g:.1f}/s"
            if peak_u is not None:
                peak_bit += f"@{peak_u}u"
            return (
                f"refine stall (no improvement vs {peak_bit}) "
                f"stopping at users={self._users}; report {peak_bit}"
            )

        self._low_ok = max(self._low_ok or 0, int(self._users))
        self._record_level_goodput(float(goodput_rps))
        self._refine_best_goodput = float(goodput_rps)
        self._refine_best_users = int(self._users)

        if int(self._users) >= int(self._p.max_users):
            self._done = True
            self._stop_reason = "max-users-reached"
            return (
                f"refine max users ceiling hit at users={self._users}; "
                f"report peak_goodput={self._refine_best_goodput:.1f}/s"
            )

        step, eff = self._refine_next_step_users(goodput_rps=float(goodput_rps))
        if step < int(self._refine_min_step or self._p.refine_min_step_users):
            self._done = True
            self._stop_reason = "refine-goodput-stall"
            return (
                f"refine stall (eff={eff:.2f} -> step={step}<min) "
                f"stopping at users={self._users}; "
                f"report peak_goodput={self._refine_best_goodput:.1f}/s"
                + (
                    f"@{self._refine_best_users}u"
                    if self._refine_best_users is not None
                    else ""
                )
            )

        prev_users = int(self._users)
        self._apply_ramp_step(int(step))
        self._users = min(int(self._p.max_users), prev_users + int(self._step))
        return (
            f"refine ramp eff={eff:.2f} × max_step={self._refine_max_step} "
            f"goodput={goodput_rps:.1f}/s fail%={fail_pct:.1f} "
            f"-> +{self._step} users={self._users}"
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
            append_latency_sample(self._lat_samples, self._read_latency_ms())
            return

        self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
        v = self._read_latency_ms()
        append_latency_sample(self._lat_samples, v)

        roll_goodput = 0.0
        win_fail_pct = 0.0
        rolled = self._rolling_goodput_and_fail_pct()
        if rolled is not None:
            roll_goodput, win_fail_pct = rolled
        self._goodput_samples.append(float(roll_goodput))
        if roll_goodput > 0 and self._phase == "explore":
            self._update_explore_peak(float(roll_goodput))
        log_sample(
            t=t,
            users=int(self._users),
            goodput_rps=roll_goodput,
            fail_pct=win_fail_pct,
            p95_ms=v,
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

        elapsed = t - self._level_start_t
        if self._is_warmup:
            if elapsed >= float(self._p.explore_warmup_duration_s):
                healthy, reason = self._warmup_health_ok()
                _LOG.info(
                    "explore-refine warmup end t=%.0fs at users=%s healthy=%s (%s) | %s",
                    t,
                    self._users,
                    healthy,
                    reason,
                    self._stats_snapshot(),
                )
                if not healthy:
                    self._done = True
                    self._stop_reason = "warmup-unhealthy"
                    _LOG.info(
                        "adaptive phase end t=%.0fs: warmup unhealthy (%s); "
                        "stopping before explore | %s",
                        t,
                        reason,
                        self._stats_snapshot(),
                    )
                    self._emit_final()
                    return None
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
            step = self._sync_explore_users()
            if step > 0:
                self._explore_last_bump_t = t
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
                    self._p.explore_min_step_users,
                    100.0 * self._p.explore_ramp_fraction,
                    int(self._users) - step,
                    ramp_interval_s,
                    p95_label,
                    self._users,
                    float(self._explore_peak_goodput),
                )

        goodput_rps, fail_pct, p95_ms, have_enough = self._rolling_decision_metrics()
        if not have_enough:
            return int(self._users), self._spawn_rate()

        if (t - self._explore_last_decision_t) < float(self._p.sample_every_s):
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

        if t >= self._next_sample_t:
            self._sample_tick(t)
            self._next_sample_t = t + float(self._p.sample_every_s)

        settle_elapsed = t - float(self._recovery_settle_start_t)
        if settle_elapsed < float(self._p.recovery_settle_duration_s):
            return int(self._users), self._spawn_rate()

        goodput_rps, fail_pct, p95_ms, have_enough = self._rolling_decision_metrics()
        if not have_enough:
            return int(self._users), self._spawn_rate()

        if self._recovery_is_healthy(
            fail_pct=float(fail_pct),
            p95_ms=p95_ms,
        ):
            action = self._begin_refine(t)
            _LOG.info(
                "adaptive phase end t=%.0fs: recovery healthy -> %s | %s | "
                "step_goodput=%.1f/s p95=%s fail%%=%.1f "
                "attempt=%s settle=%.0fs",
                t,
                action,
                self._stats_snapshot(),
                goodput_rps,
                f"{p95_ms:.0f}ms" if p95_ms is not None else "n/a",
                fail_pct,
                self._recovery_attempt,
                settle_elapsed,
            )
            return int(self._users), self._spawn_rate()

        action = self._recovery_retry_or_refine(
            t,
            fail_pct=float(fail_pct),
            p95_ms=p95_ms,
        )
        _LOG.info(
            "adaptive phase end t=%.0fs: recovery unhealthy -> %s | %s | "
            "step_goodput=%.1f/s p95=%s fail%%=%.1f",
            t,
            action,
            self._stats_snapshot(),
            goodput_rps,
            f"{p95_ms:.0f}ms" if p95_ms is not None else "n/a",
            fail_pct,
        )
        if self._done:
            self._emit_final()
            return None
        return int(self._users), self._spawn_rate()

    def _tick_refine(self):
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

        if not self._spawn_complete(t):
            return int(self._users), self._spawn_rate()

        if t >= self._next_sample_t:
            self._sample_tick(t)
            self._next_sample_t = t + float(self._p.sample_every_s)

        elapsed = t - self._level_start_t
        if elapsed < self._min_level_duration_s():
            return int(self._users), self._spawn_rate()

        n = self._decision_window_size()
        recent_goodput = self._goodput_samples[-n:]
        have_enough = len(recent_goodput) >= n
        at_cap = elapsed >= self._max_level_duration_s()
        if not have_enough and not at_cap:
            return int(self._users), self._spawn_rate()

        if not have_enough:
            _LOG.warning(
                "adaptive phase end t=%.0fs: insufficient refine goodput samples "
                "(%d/%d) at users=%s after %ds | %s",
                t,
                len(recent_goodput),
                n,
                self._users,
                int(elapsed),
                self._stats_snapshot(),
            )
            self._enter_level(t)
            return int(self._users), self._spawn_rate()

        thr = float(self._p.refine_goodput_stability_pct)
        spread = _max_deviation_from_mean_pct(recent_goodput)
        stable = spread <= thr
        if have_enough and not stable and not at_cap:
            return int(self._users), self._spawn_rate()

        rolled = self._rolling_goodput_and_fail_pct()
        fail_pct = float(rolled[1]) if rolled is not None else 0.0
        level_goodput_rps = float(statistics.mean(recent_goodput))
        if rolled is None:
            _LOG.warning(
                "adaptive: rolling rates unavailable at refine decision; "
                "fail%% forced to 0.0"
            )

        action = self._decide_refine(
            fail_pct=fail_pct,
            goodput_rps=level_goodput_rps,
            stable=bool(stable),
        )
        _LOG.info(
            "adaptive phase end t=%.0fs: %s | %s | step_goodput=%.1f/s "
            "stability_spread=%.1f%% fail%%=%.1f "
            "goodput_window=%s",
            t,
            action,
            self._stats_snapshot(),
            level_goodput_rps,
            spread,
            fail_pct,
            [round(v, 1) for v in recent_goodput],
        )
        if self._done:
            self._emit_final()
            return None
        # Wait-for-stable does not advance the user level.
        if action.startswith("refine wait"):
            return int(self._users), self._spawn_rate()
        self._enter_level(t)
        return int(self._users), self._spawn_rate()

    def tick(self):
        self._last_tick_t = float(self.get_run_time())
        if self._phase == "explore":
            return self._tick_explore()
        if self._phase == "recovery":
            return self._tick_recovery()
        return self._tick_refine()

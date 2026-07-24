from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from .base import BaseShape
from .manifest import _load_manifest
from .metrics import (
    _sample_spread_pct,
    active_user_count,
    append_latency_sample,
    current_rates,
    goodput_efficiency,
    level_goodput_from_ticks,
    level_window_counts,
    log_sample,
    ramp_spawn_caught_up,
    read_latency_ms,
    rolling_goodput_and_fail_pct,
    spawn_rate_for_step,
    spawn_settle_s,
    stats_snapshot,
    totals,
)

_LOG = logging.getLogger("baxbench.adaptive")


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
        step_up_gain=max(1.0, float(cfg.get("step_up_gain", 1.5))),
        efficiency_good_threshold=max(
            0.0, min(1.0, float(cfg.get("efficiency_good_threshold", 0.95)))
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
        plateau_stop_steps=max(
            1,
            int(cfg.get("plateau_stop_steps", cfg.get("refine_stop_steps", 2))),
        ),
        plateau_goodput_threshold_pct=float(
            cfg.get("plateau_goodput_threshold_pct", 5.0)
        ),
        overload_backoff_max=max(1, int(cfg.get("overload_backoff_max", 2))),
        spawn_target_duration_s=max(1.0, float(cfg["spawn_target_duration_s"])),
        spawn_settle_buffer_s=max(0.0, float(cfg["spawn_settle_buffer_s"])),
        health_grace_s=max(5, int(cfg["health_grace_s"])),
        abort_on_no_users=bool(cfg["abort_on_no_users"]),
    )


class GoodputPlateauShape(BaseShape):
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

        self._lat_samples: list[float] = []
        self._goodput_samples: list[float] = []

        self._level_start_reqs = 0
        self._level_start_fails = 0
        self._window_start_reqs: int | None = None
        self._window_start_fails: int | None = None
        self._sample_ticks: list[tuple[float, int, int]] = []
        self._is_warmup = self._p.warmup_step_duration_s > 0

        self._goodput_history: list[tuple[int, float]] = []
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

    def _totals(self) -> tuple[int, int]:
        return totals(self)

    def _current_rates(self) -> tuple[float, float] | None:
        return current_rates(self)

    def _rolling_goodput_and_fail_pct(self) -> tuple[float, float] | None:
        return rolling_goodput_and_fail_pct(self)

    def _read_latency_ms(self) -> float | None:
        return read_latency_ms(self, self._p.quantile)

    def _stats_snapshot(self) -> str:
        return stats_snapshot(self, self._p.quantile)

    def _active_user_count(self) -> int | None:
        return active_user_count(self)

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
        return spawn_settle_s(
            delta_users=int(getattr(self, "_level_delta_users", 0)),
            spawn_rate=self._spawn_rate(),
            buffer_s=float(self._p.spawn_settle_buffer_s),
        )

    def _spawn_complete(self, t: float) -> bool:
        delta = int(getattr(self, "_level_delta_users", 0))
        if delta <= 0:
            return True
        elapsed = t - self._level_start_t
        if elapsed < self._spawn_settle_s():
            return False
        return ramp_spawn_caught_up(
            active=self._active_user_count(),
            target_users=int(self._users),
            delta_users=delta,
            tolerance_floor=2,
        )

    def _decision_window_size(self) -> int:
        return int(self._p.min_settle_samples)

    def _decision_have_enough(
        self, *, recent_lat: list[float], recent_ticks: list, recent_goodput: list[float]
    ) -> bool:
        n = self._decision_window_size()
        return len(recent_lat) >= n and len(recent_ticks) >= n + 1

    def _decision_is_stable(
        self, *, recent_lat: list[float], recent_goodput: list[float]
    ) -> tuple[bool, float]:
        spread = _sample_spread_pct(recent_lat)
        thr = float(self._p.stability_drift_threshold_pct)
        return spread <= thr, spread

    def _decision_goodput_rps(
        self,
        *,
        rolled: tuple[float, float] | None,
        recent_goodput: list[float],
    ) -> tuple[float, float]:
        if rolled is not None:
            return float(rolled[0]), float(rolled[1])
        n = self._decision_window_size()
        window = recent_goodput[-n:] if recent_goodput else []
        goodput = (
            float(statistics.mean(window))
            if window
            else float(self._level_goodput_from_window())
        )
        return goodput, 0.0

    def _level_goodput_from_window(self) -> float:
        n = self._decision_window_size()
        return level_goodput_from_ticks(self._sample_ticks[-(n + 1) :])

    def _level_window_counts(self) -> tuple[int, int]:
        n = self._decision_window_size()
        return level_window_counts(self._sample_ticks[-(n + 1) :])

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
        return spawn_rate_for_step(
            delta_users=int(getattr(self, "_level_delta_users", 0)),
            target_duration_s=float(self._p.spawn_target_duration_s),
            ceiling=int(self._p.spawn_rate),
            current_users=int(self._users),
        )

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
        self._goodput_history.append((int(self._users), float(goodput_rps)))
        self._best_goodput = max(self._best_goodput, float(goodput_rps))

    def _emit_final(self) -> None:
        if self._final_logged or self._stop_reason is None:
            return
        self._final_logged = True
        history = (
            ", ".join(f"{u}u:{g:.1f}/s" for u, g in self._goodput_history[-8:])
            or "(none)"
        )
        _LOG.info(
            "adaptive-v2 stop: reason=%s final_users=%s low_ok=%s high_bad=%s "
            "goodput_history=[%s]",
            self._stop_reason,
            self._users,
            self._low_ok,
            self._high_bad,
            history,
        )

    def _backoff_drop_users(self) -> int:
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
            overloaded_reasons.append(
                f"p95={p95_ms:.0f}>{float(self._p.overload_p95_ms):.0f}ms"
            )
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
            return (
                f"goodput ramp recovery{stab_note} -> users={self._users} step={self._step}"
            )

        prev_users, prev_goodput = last_stable
        eff = goodput_efficiency(
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

        if t >= self._next_sample_t:
            elapsed = t - self._level_start_t
            if elapsed >= self._measure_start_s():
                reqs_now, fails_now = self._totals()
                if self._window_start_reqs is None:
                    self._window_start_reqs = reqs_now
                    self._window_start_fails = fails_now
                    self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
                    append_latency_sample(self._lat_samples, self._read_latency_ms())
                else:
                    self._sample_ticks.append((float(t), int(reqs_now), int(fails_now)))
                    v = self._read_latency_ms()
                    append_latency_sample(self._lat_samples, v)

                    roll_goodput = 0.0
                    win_fail_pct = 0.0
                    rolled = self._rolling_goodput_and_fail_pct()
                    if rolled is not None:
                        roll_goodput, win_fail_pct = rolled
                    self._goodput_samples.append(float(roll_goodput))
                    log_sample(
                        t=t,
                        users=int(self._users),
                        goodput_rps=roll_goodput,
                        fail_pct=win_fail_pct,
                        p95_ms=v,
                    )

            self._next_sample_t = t + float(self._p.sample_every_s)

        elapsed = t - self._level_start_t

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

        if elapsed < self._min_level_duration_s():
            return int(self._users), self._spawn_rate()

        n = self._decision_window_size()
        recent_lat = self._lat_samples[-n:]
        recent_ticks = self._sample_ticks[-(n + 1) :]
        recent_goodput = self._goodput_samples[-n:]
        have_enough = self._decision_have_enough(
            recent_lat=recent_lat,
            recent_ticks=recent_ticks,
            recent_goodput=recent_goodput,
        )
        stable, stability_spread_pct = self._decision_is_stable(
            recent_lat=recent_lat, recent_goodput=recent_goodput
        )

        at_cap = elapsed >= self._max_level_duration_s()
        if not have_enough and not at_cap:
            return int(self._users), self._spawn_rate()
        if have_enough and not stable and not at_cap:
            return int(self._users), self._spawn_rate()

        if not have_enough:
            _LOG.warning(
                "adaptive phase end t=%.0fs: insufficient decision-window samples "
                "(p95=%d/%d goodput=%d/%d ticks=%d/%d) at users=%s after %ds | %s",
                t,
                len(recent_lat),
                n,
                len(recent_goodput),
                n,
                len(recent_ticks),
                n + 1,
                self._users,
                int(elapsed),
                self._stats_snapshot(),
            )
            self._enter_level(t)
            return int(self._users), self._spawn_rate()

        rolled = self._rolling_goodput_and_fail_pct()
        level_goodput_rps, fail_pct = self._decision_goodput_rps(
            rolled=rolled, recent_goodput=recent_goodput
        )
        if rolled is None:
            _LOG.warning(
                "adaptive: rolling rates unavailable at decision; "
                "fail%% forced to 0.0 (goodput fallback samples=%s)",
                len(recent_goodput),
            )
        step_p95_ms = max(recent_lat) if recent_lat else None
        action = self._decide_and_advance(
            fail_pct=float(fail_pct),
            goodput_rps=float(level_goodput_rps),
            stable=bool(stable),
            p95_ms=step_p95_ms,
        )
        _LOG.info(
            "adaptive phase end t=%.0fs: %s | %s | step_goodput=%.1f/s "
            "stability_spread=%.1f%% fail%%=%.1f "
            "goodput_window=%s samples=%s",
            t,
            action,
            self._stats_snapshot(),
            level_goodput_rps,
            stability_spread_pct,
            fail_pct,
            [round(v, 1) for v in recent_goodput],
            [round(v, 1) for v in recent_lat],
        )
        if self._done:
            self._emit_final()
            return None
        self._enter_level(t)
        return int(self._users), self._spawn_rate()

"""Shared Locust stats / sampling helpers for adaptive load shapes."""

from __future__ import annotations

import logging
import math
import statistics

_LOG = logging.getLogger("baxbench.adaptive")


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


def _max_deviation_from_mean_pct(samples: list[float]) -> float:
    """Max |x - mean| / mean across samples, as a percentage (0–100+)."""
    if len(samples) < 2:
        return 0.0
    mean = float(statistics.mean(samples))
    if mean <= 0:
        return 100.0 if any(float(x) > 0 for x in samples) else 0.0
    return max(abs(float(x) - mean) / mean * 100.0 for x in samples)


def locust_environment(shape) -> object | None:
    env = getattr(shape, "environment", None)
    if env is not None:
        return env
    runner = getattr(shape, "runner", None)
    return getattr(runner, "environment", None) if runner is not None else None


def active_user_count(shape) -> int | None:
    runner = getattr(shape, "runner", None)
    if runner is None:
        return None
    try:
        return int(shape.get_current_user_count())
    except Exception:
        return None


def totals(shape) -> tuple[int, int]:
    env = locust_environment(shape)
    total = getattr(getattr(env, "stats", None), "total", None) if env else None
    if total is None:
        return 0, 0
    reqs = int(getattr(total, "num_requests", 0) or 0)
    fails = int(getattr(total, "num_failures", 0) or 0)
    return reqs, fails


def current_rates(shape) -> tuple[float, float] | None:
    """(current_rps, current_fail_per_sec) from Locust trailing window."""
    env = locust_environment(shape)
    total = getattr(getattr(env, "stats", None), "total", None) if env else None
    if total is None:
        return None
    try:
        rps = float(getattr(total, "current_rps", 0.0) or 0.0)
        failps = float(getattr(total, "current_fail_per_sec", 0.0) or 0.0)
    except Exception:
        return None
    return rps, failps


def rolling_goodput_and_fail_pct(shape) -> tuple[float, float] | None:
    """Locust rolling-window goodput and fail% (same slice as sample logs).

    Returns ``None`` when Locust rates are unavailable. Callers should not fall
    back to lifetime totals — those poison early startup errors forever.
    """
    rates = current_rates(shape)
    if rates is None:
        return None
    rps, failps = rates
    goodput = max(0.0, float(rps) - float(failps))
    fail_pct = (100.0 * float(failps) / float(rps)) if float(rps) > 0.0 else 0.0
    return goodput, fail_pct


def read_latency_ms(
    shape,
    quantile: float,
    *,
    warn_once_attr: str = "_window_unavailable_logged",
) -> float | None:
    """Current response-time percentile in ms (windowed, else cumulative)."""
    env = locust_environment(shape)
    total = getattr(getattr(env, "stats", None), "total", None) if env else None
    if total is None:
        return None
    v = None
    try:
        v = total.get_current_response_time_percentile(float(quantile))
    except Exception:
        v = None
    if v is None:
        if not getattr(shape, warn_once_attr, False):
            setattr(shape, warn_once_attr, True)
            _LOG.warning(
                "adaptive: windowed p%s unavailable; falling back to "
                "cumulative percentile (per-step latency may be polluted by history)",
                int(float(quantile) * 100),
            )
        try:
            v = total.get_response_time_percentile(float(quantile))
        except Exception:
            return None
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def stats_snapshot(shape, quantile: float) -> str:
    """Compact request/latency/goodput string for adaptive phase logs."""
    reqs, fails = totals(shape)
    lat = read_latency_ms(shape, quantile)
    lat_s = f"{lat:.0f}ms" if lat is not None else "n/a"
    rolled = rolling_goodput_and_fail_pct(shape)
    q = int(float(quantile) * 100)
    if rolled is not None:
        goodput, fail_pct = rolled
        rates = current_rates(shape) or (0.0, 0.0)
        return (
            f"reqs={reqs} fail={fails} "
            f"roll={rates[0]:.1f}rps goodput={goodput:.1f}/s "
            f"fail%={fail_pct:.1f}% "
            f"p{q}={lat_s}"
        )
    lifetime_fail = f"{100.0 * fails / reqs:.1f}%" if reqs else "n/a"
    return f"reqs={reqs} fail={fails} ({lifetime_fail}) p{q}={lat_s}"


def goodput_efficiency(
    prev_users: int, prev_goodput: float, users: int, goodput: float
) -> float:
    """Marginal goodput gain per relative user increase (dimensionless)."""
    if prev_users <= 0 or prev_goodput <= 0:
        return 0.0
    if users <= prev_users:
        return 0.0
    d_goodput_frac = (goodput - prev_goodput) / prev_goodput
    d_users_frac = (users - prev_users) / prev_users
    if d_users_frac <= 0:
        return 0.0
    return d_goodput_frac / d_users_frac


def level_goodput_from_ticks(ticks: list[tuple[float, int, int]]) -> float:
    """Mean successful RPS over counter boundaries (not per-tick deltas)."""
    if len(ticks) < 2:
        return 0.0
    t0, r0, f0 = ticks[0]
    t1, r1, f1 = ticks[-1]
    dt = max(0.001, float(t1) - float(t0))
    succ0 = max(0, int(r0) - int(f0))
    succ1 = max(0, int(r1) - int(f1))
    return max(0.0, float(succ1 - succ0) / dt)


def level_window_counts(ticks: list[tuple[float, int, int]]) -> tuple[int, int]:
    """(requests, failures) between first and last tick boundaries."""
    if len(ticks) < 2:
        return 0, 0
    _t0, r0, f0 = ticks[0]
    _t1, r1, f1 = ticks[-1]
    return max(0, int(r1) - int(r0)), max(0, int(f1) - int(f0))


def spawn_settle_s(
    *, delta_users: int, spawn_rate: int, buffer_s: float
) -> float:
    """Seconds to wait after a ramp-up before measuring (spawn + buffer)."""
    if int(delta_users) <= 0:
        return 0.0
    rate = max(1, int(spawn_rate))
    return float(delta_users) / float(rate) + float(buffer_s)


def spawn_rate_for_step(
    *,
    delta_users: int,
    target_duration_s: float,
    ceiling: int,
    current_users: int,
) -> int:
    """Spawn rate that lands ``delta_users`` in about ``target_duration_s``."""
    if int(delta_users) <= 0:
        return max(1, min(int(ceiling), int(current_users)))
    target_dur = max(0.001, float(target_duration_s))
    rate_for_step = max(1, int(math.ceil(float(delta_users) / target_dur)))
    return max(1, min(int(ceiling), rate_for_step, int(current_users)))


def ramp_spawn_caught_up(
    *,
    active: int | None,
    target_users: int,
    delta_users: int,
    tolerance_floor: int = 2,
) -> bool:
    """True when active users have reached the current shape target."""
    if active is None or int(target_users) <= 0:
        return True
    if int(delta_users) <= 0:
        return True
    tolerance = max(int(tolerance_floor), int(0.02 * int(target_users)))
    return int(active) >= int(target_users) - tolerance


def append_latency_sample(samples: list[float], latency_ms: float | None) -> None:
    if latency_ms is not None and float(latency_ms) > 0:
        samples.append(float(latency_ms))


def log_sample(
    *,
    t: float,
    users: int,
    goodput_rps: float,
    fail_pct: float,
    p95_ms: float | None,
) -> None:
    if p95_ms is None or float(p95_ms) <= 0:
        return
    _LOG.info(
        "sample t=%.0fs users=%s goodput=%.1f/s fail_pct=%.2f%% p95=%.0fms",
        t,
        users,
        goodput_rps,
        fail_pct,
        p95_ms,
    )

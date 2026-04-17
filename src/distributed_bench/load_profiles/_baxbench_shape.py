from __future__ import annotations

import os

from locust import LoadTestShape, between


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


class BaxbenchShape(LoadTestShape):
    """
    Shared load-shape for baxbench, configured via env vars injected by LocustRunner.

    Modes:
      - steady: hold a constant number of users
      - continuous: linearly ramp from start -> target over runtime
      - stairs: step-wise increases
      - spike: periodic spikes on top of a base load
    """

    def tick(self):
        mode = (os.getenv("BAXBENCH_LOAD_MODE", "steady") or "steady").strip().lower()

        run_time_s = max(1, _get_int("BAXBENCH_RUN_TIME_S", 1))

        t = float(self.get_run_time())
        if t >= float(run_time_s):
            return None

        if mode == "steady":
            steady_users = max(0, _get_int("BAXBENCH_STEADY_USERS", 0))
            return steady_users, max(1, steady_users)

        if mode == "continuous":
            spawn_rate = max(1, _get_int("BAXBENCH_CONTINUOUS_SPAWN_RATE", 1))
            start = max(0, _get_int("BAXBENCH_CONTINUOUS_START_USERS", 0))
            target = max(start, _get_int("BAXBENCH_CONTINUOUS_TARGET_USERS", start))
            if run_time_s <= 1:
                return target, spawn_rate
            frac = min(1.0, max(0.0, t / float(run_time_s)))
            users = int(round(start + (target - start) * frac))
            return max(0, users), spawn_rate

        if mode == "stairs":
            start = max(0, _get_int("BAXBENCH_STAIRS_START_USERS", 0))
            step_users = max(0, _get_int("BAXBENCH_STAIRS_STEP_USERS", 100))
            step_dur = max(1, _get_int("BAXBENCH_STAIRS_STEP_DURATION_S", 30))
            steps = max(1, _get_int("BAXBENCH_STAIRS_STEPS", 10))
            idx = int(t // float(step_dur))
            idx = min(idx, steps)  # allow the last step to persist until run_time_s
            users = start + (step_users * idx)
            spawn_rate = max(1, step_users)
            return max(0, users), spawn_rate

        if mode == "spike":
            base = max(0, _get_int("BAXBENCH_SPIKE_BASE_USERS", 500))
            spike = max(base, _get_int("BAXBENCH_SPIKE_USERS", 1000))
            interval = max(1, _get_int("BAXBENCH_SPIKE_INTERVAL_S", 30))
            dur = max(1, _get_int("BAXBENCH_SPIKE_DURATION_S", 10))

            in_spike = (t % float(interval)) < float(dur)
            users = spike if in_spike else base
            spawn_rate = max(1, abs(spike - base))
            return max(0, users), spawn_rate

        # Unknown mode: fall back to steady to avoid a "silent no-load" run.
        steady_users = max(0, _get_int("BAXBENCH_STEADY_USERS", 0))
        return steady_users, max(1, steady_users)


from __future__ import annotations

from .base import BaseShape
from .manifest import _load_manifest

class SteadyShape(BaseShape):
    def tick(self):
        if self._should_stop():
            return None
        cfg = _load_manifest()
        steady_users = max(0, int(cfg.get("steady_users", cfg["users"])))
        return steady_users, max(1, steady_users)


class ContinuousShape(BaseShape):
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


class StairsShape(BaseShape):
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


class SpikeShape(BaseShape):
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

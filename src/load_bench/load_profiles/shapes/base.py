from __future__ import annotations

import logging

from locust import LoadTestShape, between

from .manifest import _load_manifest

_LOG = logging.getLogger("baxbench.adaptive")


def baxbench_wait_time():
    """Locust wait_time callable configured via the load profile manifest."""
    cfg = _load_manifest()
    wmin = float(cfg["wait_min_s"])
    wmax = float(cfg["wait_max_s"])
    lo, hi = (wmin, wmax) if wmin <= wmax else (wmax, wmin)
    return between(lo, hi)


class BaseShape(LoadTestShape):
    def _should_stop(self) -> bool:
        run_time_s = max(1, int(_load_manifest()["run_time_s"]))
        return float(self.get_run_time()) >= float(run_time_s)


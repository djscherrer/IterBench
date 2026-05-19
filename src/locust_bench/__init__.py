"""
Locust load generation for BaxBench.

- ``load_profiles/`` — named load shapes (steady, stairs, …) and ``_baxbench_shape.py``
- ``runner.py`` — ``LocustRunner``: run Locust on **remote SSH load hosts** (distributed bench)
- ``local_runner.py`` — ``run_headless_locust``: run Locust **on this machine** (k8s-bench, local docker bench)
"""

from .load_profiles import (
    LOAD_PROFILE_REGISTRY,
    AdaptiveLoadProfile,
    BaseLoadProfile,
    ContinuousLoadProfile,
    LoadProfile,
    SpikeLoadProfile,
    StairsLoadProfile,
    SteadyLoadProfile,
    resolve_load_profile,
)
from .local_runner import resolve_locust_user_class, run_headless_locust
from .runner import LocustRunner

__all__ = [
    "LOAD_PROFILE_REGISTRY",
    "AdaptiveLoadProfile",
    "BaseLoadProfile",
    "ContinuousLoadProfile",
    "LoadProfile",
    "SpikeLoadProfile",
    "StairsLoadProfile",
    "SteadyLoadProfile",
    "LocustRunner",
    "resolve_load_profile",
    "resolve_locust_user_class",
    "run_headless_locust",
]

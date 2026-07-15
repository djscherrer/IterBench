"""Locust LoadTestShape implementations for BaxBench load profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .adaptive import AdaptiveShape
from .adaptive_v2 import AdaptiveV2Shape
from .base import BaseShape, baxbench_wait_time
from .explore_refine import ExploreRefineShape
from .goodput_plateau import GoodputPlateauShape
from .manifest import _load_manifest
from .simple import ContinuousShape, SpikeShape, StairsShape, SteadyShape

if TYPE_CHECKING:
    pass

__all__ = [
    "AdaptiveShape",
    "AdaptiveV2Shape",
    "BaseShape",
    "BaxbenchShape",
    "ContinuousShape",
    "ExploreRefineShape",
    "GoodputPlateauShape",
    "SpikeShape",
    "StairsShape",
    "SteadyShape",
    "baxbench_wait_time",
    "selected_shape_class",
]


def selected_shape_class() -> type[BaseShape]:
    mode = (_load_manifest().get("mode") or "steady").strip().lower()
    mapping: dict[str, type[BaseShape]] = {
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


def __getattr__(name: str):
    # Resolve the active shape lazily so importing helpers does not require a
    # staged manifest (Locustfiles still get BaxbenchShape at attribute access).
    if name == "BaxbenchShape":
        return selected_shape_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

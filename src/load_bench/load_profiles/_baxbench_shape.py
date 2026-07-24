"""
Locust-facing facade for BaxBench load shapes.

Scenario locustfiles keep:

    from _baxbench_shape import BaxbenchShape, baxbench_wait_time

Implementation lives in the ``shapes/`` package (staged beside this file for
distributed Locust runs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from .shapes import (
        AdaptiveShape,
        AdaptiveV2Shape,
        BaseShape,
        ContinuousShape,
        ExploreRefineShape,
        GoodputPlateauShape,
        SpikeShape,
        StairsShape,
        SteadyShape,
        baxbench_wait_time,
        selected_shape_class,
    )
except ImportError:  # pragma: no cover - Locust staging (flat cwd, no package parent)
    from shapes import (
        AdaptiveShape,
        AdaptiveV2Shape,
        BaseShape,
        ContinuousShape,
        ExploreRefineShape,
        GoodputPlateauShape,
        SpikeShape,
        StairsShape,
        SteadyShape,
        baxbench_wait_time,
        selected_shape_class,
    )

# Back-compat alias used by older call sites / copied shape bundles.
_BaseShape = BaseShape
_selected_shape_class = selected_shape_class

if TYPE_CHECKING:
    BaxbenchShape = BaseShape

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


def __getattr__(name: str):
    if name == "BaxbenchShape":
        return selected_shape_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

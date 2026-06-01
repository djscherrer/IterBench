"""
Per-iteration stages of the k8s benchmark loop.

Each stage takes ``(ctx, plan, cfg, logger)`` (plus stage-specific extras) and
performs one well-defined step in an iteration:

- :mod:`code`    – code refinement (LLM) + functional tests; returns new image.
- :mod:`spec`    – produce ``spec.yaml`` (baseline / reuse / generate+probe).
- :mod:`bench`   – Locust load test against the deployed iteration.
- :mod:`outcome` – collect feedback, write artifacts, append summary block.
"""

from __future__ import annotations

from .bench import run_bench, run_locust_for_iteration
from .code import refine_code_or_fail
from .outcome import record_outcome
from .spec import prepare_spec_or_fail

__all__ = [
    "prepare_spec_or_fail",
    "record_outcome",
    "refine_code_or_fail",
    "run_bench",
    "run_locust_for_iteration",
]

"""
Keep package import lightweight.

Some submodules depend on optional runtime deps (e.g. docker). Importing them here makes
*any* `distributed_bench.*` import fail in environments where those deps aren't installed
(including analysis-only workflows).
"""

from __future__ import annotations

from typing import Any


def run_remote_bench(*args: Any, **kwargs: Any) -> Any:
    from .orchestrator import run_remote_bench as _run_remote_bench

    return _run_remote_bench(*args, **kwargs)


def run_remote_preflight(*args: Any, **kwargs: Any) -> Any:
    from .preflight import run_remote_preflight as _run_remote_preflight

    return _run_remote_preflight(*args, **kwargs)


def run_preflight_from_args(*args: Any, **kwargs: Any) -> Any:
    from .preflight import run_preflight_from_args as _run_preflight_from_args

    return _run_preflight_from_args(*args, **kwargs)


__all__ = [
    "run_remote_bench",
    "run_remote_preflight",
    "run_preflight_from_args",
]

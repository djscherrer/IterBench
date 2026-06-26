"""
Failure handling for k8s iterative experiments.

- :func:`fail_iteration_phase` — mark a phase failed, rename folder, update summary
- :func:`build_functional_failure_report` — parse FT logs into structured diagnostics
"""

from __future__ import annotations

from .build import build_functional_failure_report
from .infra import InfrastructureFailure, detect_infrastructure_failure
from .models import FunctionalFailure, FunctionalFailureReport
from .phase import fail_iteration_phase

__all__ = [
    "FunctionalFailure",
    "FunctionalFailureReport",
    "InfrastructureFailure",
    "build_functional_failure_report",
    "detect_infrastructure_failure",
    "fail_iteration_phase",
]

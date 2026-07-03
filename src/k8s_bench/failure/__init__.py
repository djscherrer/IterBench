"""
Failure handling for k8s iterative experiments.

- :func:`fail_iteration_phase` — mark a phase failed, persist ``failure.json``
- :class:`FailureRecord` / :class:`IterationFailure` — structured failure model
- Attempt-scoped failures under ``attempts/NNN/failure.json``
"""

from __future__ import annotations

from .build import build_code_failure_record, docker_build_failed_in_test_log
from .failure_models import FunctionalFailure, InfrastructureFailure
from .infra import classify_ft_failure
from .persist import (
    build_code_iteration_failure,
    code_attempt_dir,
    load_prior_code_attempt_failure,
    load_terminal_failure_record,
    write_attempt_failure,
)
from .phase import fail_iteration_phase
from .record import FailureKind, FailureRecord, IterationFailure, Phase

__all__ = [
    "FailureKind",
    "FailureRecord",
    "FunctionalFailure",
    "InfrastructureFailure",
    "IterationFailure",
    "Phase",
    "build_code_failure_record",
    "build_code_iteration_failure",
    "classify_ft_failure",
    "code_attempt_dir",
    "docker_build_failed_in_test_log",
    "fail_iteration_phase",
    "load_prior_code_attempt_failure",
    "load_terminal_failure_record",
    "write_attempt_failure",
]

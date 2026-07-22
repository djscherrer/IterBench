"""
Failure handling for k8s iterative experiments.

- :func:`fail_iteration_phase` — mark a phase failed, persist ``failure.json``
- Phase-specific failure records + :class:`IterationFailure` envelope
- Attempt-scoped failures under ``attempts/NNN/failure.json``
"""

from __future__ import annotations

from failure import FailureRecord, RetryTarget

from .build import build_code_failure_record, docker_build_failed_in_test_log
from .infra import classify_ft_failure
from .persist import (
    build_bench_iteration_failure,
    build_code_iteration_failure,
    build_deploy_iteration_failure,
    build_spec_iteration_failure,
    code_attempt_dir,
    load_prior_code_attempt_failure,
    load_prior_iteration_failure,
    load_terminal_failure_record,
    write_attempt_failure,
)
from .phase import fail_iteration_phase
from .record import (
    BenchFailureKind,
    BenchFailureRecord,
    CodeFailureRecord,
    DecisionFailureRecord,
    DeployFailureKind,
    DeployFailureRecord,
    IterationFailure,
    Phase,
    SpecFailureRecord,
)

FunctionalFailure = CodeFailureRecord.FunctionalFailure
InfrastructureFailure = CodeFailureRecord.InfrastructureFailure

__all__ = [
    "BenchFailureKind",
    "BenchFailureRecord",
    "CodeFailureRecord",
    "DecisionFailureRecord",
    "DeployFailureKind",
    "DeployFailureRecord",
    "FailureRecord",
    "FunctionalFailure",
    "InfrastructureFailure",
    "IterationFailure",
    "Phase",
    "RetryTarget",
    "SpecFailureRecord",
    "build_bench_iteration_failure",
    "build_code_failure_record",
    "build_code_iteration_failure",
    "build_deploy_iteration_failure",
    "build_spec_iteration_failure",
    "classify_ft_failure",
    "code_attempt_dir",
    "docker_build_failed_in_test_log",
    "fail_iteration_phase",
    "load_prior_code_attempt_failure",
    "load_prior_iteration_failure",
    "load_terminal_failure_record",
    "write_attempt_failure",
]

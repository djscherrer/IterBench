"""Static validation of K8s workload specs against cluster capacity and scheduling rules.

Individual rules live in :mod:`k8s_bench.spec.validators` and are composed by
:func:`validate_spec_against_cluster`. Placement name resolution runs earlier in
:mod:`k8s_bench.spec.placement`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..cluster.capacity import ClusterCapacity
from .models import K8sWorkloadSpec
from .validators import (
    SPEC_VALIDATORS,
    effective_pool_max,
    estimate_app_client_connections,
    validate_backend_env,
    validate_database_connections,
)

__all__ = [
    "SpecValidationError",
    "SpecValidationResult",
    "effective_pool_max",
    "estimate_app_client_connections",
    "validate_backend_env",
    "validate_database_connections",
    "validate_spec_against_cluster",
]


@dataclass(frozen=True)
class SpecValidationResult:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class SpecValidationError(ValueError):
    """Hard scheduling / capacity violations; safe to feed back to the LLM."""

    def __init__(self, errors: list[str], warnings: list[str] | None = None) -> None:
        self.errors = list(errors)
        self.warnings = list(warnings or [])
        super().__init__("\n".join(self.errors))

    def to_prompt_text(self) -> str:
        lines = [
            "## Spec validation failed (fix these before deploy)",
            "",
            "The previous YAML could not be scheduled on this cluster:",
            "",
        ]
        lines.extend(f"- {e}" for e in self.errors)
        if self.warnings:
            lines.extend(["", "### Warnings", *[f"- {w}" for w in self.warnings]])
        lines.extend(
            [
                "",
                "Remember: **each pod** must fit on **one worker** using **requests**. "
                "Postgres primary is one pod; read replicas are separate pods (one node each).",
            ]
        )
        return "\n".join(lines)


def validate_spec_against_cluster(
    spec: K8sWorkloadSpec,
    capacity: ClusterCapacity,
) -> SpecValidationResult:
    """Run every rule in :data:`k8s_bench.spec.validators.SPEC_VALIDATORS`."""
    errors: list[str] = []
    warnings: list[str] = []
    for validator in SPEC_VALIDATORS:
        rule_errors, rule_warnings = validator(spec, capacity)
        errors.extend(rule_errors)
        warnings.extend(rule_warnings)
    return SpecValidationResult(errors=errors, warnings=warnings)

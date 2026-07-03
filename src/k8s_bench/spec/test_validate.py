"""Tests for static k8s workload spec validation."""

from __future__ import annotations

import unittest
from pathlib import Path

from k8s_bench.cluster.capacity import ClusterCapacity, NodeCapacity
from k8s_bench.failure.record import FailureRecord
from k8s_bench.spec.models import K8sWorkloadSpec
from k8s_bench.spec.validate import SpecValidationError, validate_spec_against_cluster

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
_INVALID_SPEC = _EXAMPLES / "invalid-spec-validation.yaml"


def _example_cluster_capacity() -> ClusterCapacity:
    worker = NodeCapacity(
        name="node3.example",
        roles=("worker",),
        schedulable=True,
        allocatable_cpu_millicores=32_000,
        allocatable_memory_bytes=64 * (2**30),
    )
    return ClusterCapacity(
        nodes=(worker,),
        ready_nodes=1,
        worker_nodes=(worker,),
        total_worker_cpu_millicores=32_000,
        total_worker_memory_bytes=64 * (2**30),
    )


class SpecValidationTests(unittest.TestCase):
    def test_invalid_example_spec_fails_validation(self) -> None:
        spec = K8sWorkloadSpec.from_yaml_file(_INVALID_SPEC)
        result = validate_spec_against_cluster(spec, _example_cluster_capacity())

        self.assertFalse(result.ok)
        self.assertTrue(
            any("backend.replicas must be >= 1" in err for err in result.errors),
            result.errors,
        )

    def test_spec_validation_error_formats_prompt_text(self) -> None:
        errors = ["backend.replicas must be >= 1"]
        text = SpecValidationError(errors).to_prompt_text()

        self.assertIn("Spec validation failed", text)
        self.assertIn("backend.replicas must be >= 1", text)

    def test_failure_record_spec_validation_prompt_block(self) -> None:
        errors = "backend.replicas must be >= 1"
        record = FailureRecord(
            phase="spec",
            kind="spec_validation",
            iteration_id="iteration-invalid",
            summary="static spec validation failed",
            validation_errors=errors,
        )
        block = record.to_prompt_block()

        self.assertIn("failed static validation", block)
        self.assertIn(errors, block)


if __name__ == "__main__":
    unittest.main()

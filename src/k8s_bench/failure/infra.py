"""Infrastructure / harness failures that block functional tests."""

from __future__ import annotations

from dataclasses import dataclass

from .patterns import INFRA_FAILURE_PATTERNS
from .text import trim


@dataclass(frozen=True)
class InfrastructureFailure:
    """
    Harness/infrastructure failure that prevented the FT run.

    When present on a functional failure report, ``failed_tests`` are blocked
    tests — they never exercised the application.
    """

    kind: str
    description: str
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "description": self.description,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "InfrastructureFailure":
        return cls(
            kind=str(data.get("kind", "")),
            description=str(data.get("description", "")),
            evidence=str(data.get("evidence", "")),
        )


def detect_infrastructure_failure(test_log: str) -> InfrastructureFailure | None:
    """Return the first infrastructure failure marker in ``test.log``, if any."""
    if not test_log:
        return None
    for line in test_log.splitlines():
        for kind, pattern, description in INFRA_FAILURE_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            detail = description
            if kind == "port_conflict":
                port = m.groupdict().get("port")
                if port:
                    detail = f"{description} (port {port})"
            return InfrastructureFailure(
                kind=kind,
                description=detail,
                evidence=trim(line.strip(), max_chars=600),
            )
    return None

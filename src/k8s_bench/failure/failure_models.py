"""Dataclasses for structured failure evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionalFailure:
    """One failing functional test with the evidence we found for it."""

    name: str
    per_test_log_tail: str = ""
    container_error_excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "per_test_log_tail": self.per_test_log_tail,
            "container_error_excerpt": self.container_error_excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FunctionalFailure":
        return cls(
            name=str(data.get("name", "")),
            per_test_log_tail=str(data.get("per_test_log_tail", "")),
            container_error_excerpt=str(data.get("container_error_excerpt", "")),
        )

    @property
    def category(self) -> str:
        """Coarse failure class derived from evidence (no oracle values)."""
        tail = (self.per_test_log_tail or "").lower()
        err = (self.container_error_excerpt or "").lower()
        blob = f"{tail}\n{err}"
        if "timed out" in blob or "timeout" in blob:
            return "timeout — endpoint did not respond in time"
        if (
            re.search(r"\b5\d\d\b", tail)
            or "traceback" in err
            or "exception" in err
            or "error:" in err
            or "panic" in err
        ):
            return "server error (5xx / unhandled exception)"
        if re.search(r"\b4\d\d\b", tail):
            return "request rejected (4xx) where success was expected"
        if "mismatch" in tail or "expected" in tail:
            return "incorrect response (wrong body / values)"
        if err.strip():
            return "application error during the request"
        return "unexpected behaviour (no explicit error captured)"


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

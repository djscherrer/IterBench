"""Shared contract implemented by all persisted failure records."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

RetryTarget = Literal["author_agent", "implementation", "infrastructure", "unknown"]


class FailureRecord(ABC):
    """Common interface for K8s-bench and scenario-builder failures.

    Concrete records stay frozen dataclasses in their own domains.  Keeping the
    common base non-dataclass preserves their existing constructors and on-disk
    JSON while still providing one shared type and common serialisation fields.
    """

    phase: str
    kind: str
    iteration_id: str
    summary: str
    attempt: int | None

    def common_dict(self, *, include_null_attempt: bool = True) -> dict[str, object]:
        """Return the stable cross-domain JSON envelope.

        ``schema_version`` deliberately stays out of the common envelope for
        now: existing K8s artifacts did not carry it and must remain readable.
        Domain records can add it when they introduce a new artifact format.
        """
        data: dict[str, object] = {
            "phase": self.phase,
            "kind": self.kind,
            "iteration_id": self.iteration_id,
            "summary": self.summary,
        }
        if include_null_attempt or self.attempt is not None:
            data["attempt"] = self.attempt
        return data

    def short_excerpt(self) -> str:
        """Compact default diagnostic for summaries and logs."""
        return self.summary

    @abstractmethod
    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe persisted representation."""

    @abstractmethod
    def to_prompt_block(self) -> str:
        """Return concise, actionable feedback suitable for an author LLM."""

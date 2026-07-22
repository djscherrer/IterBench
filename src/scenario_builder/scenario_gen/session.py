"""In-memory author conversations and durable failure routing for scenario generation."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from llm import Conversation

from .failure import ScenarioGenerationFailureRecord, persist_generation_failure


def _cache_key(run_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{run_id}\x1f{name}".encode("utf-8")).hexdigest()[:24]
    return f"scenario-builder:scenario-generation:{digest}"


@dataclass
class ScenarioGenerationSession:
    """One pre-title run with in-memory authors and persisted diagnostics only.

    A candidate and its raw conversation are provisional until novelty, OpenAPI,
    and text-spec validation all succeed. They therefore remain in memory. A
    persisted record represents only a confirmed failure observation.
    """

    root: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    _conversations: dict[str, Conversation] = field(default_factory=dict)

    def conversation(self, name: str, *, system_prompt: str) -> Conversation:
        if name in self._conversations:
            return self._conversations[name]
        conversation = Conversation(
            system_prompt=system_prompt, cache_key=_cache_key(self.run_id, name)
        )
        self._conversations[name] = conversation
        return conversation

    def persist_failure(self, record: ScenarioGenerationFailureRecord) -> str:
        return persist_generation_failure(self.root, self.run_id, record)

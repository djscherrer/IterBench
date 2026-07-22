"""Durable author conversations and failure routing for scenario generation."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from llm import Conversation
from scenario_builder.conversation_store import load_conversation, persist_conversation
from workspace.scenario_builder_paths import (
    generation_conversation_path,
    scenario_generation_conversation_path,
)

from .failure import ScenarioGenerationFailureRecord, persist_generation_failure


def _cache_key(run_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{run_id}\x1f{name}".encode("utf-8")).hexdigest()[:24]
    return f"scenario-builder:scenario-generation:{digest}"


@dataclass
class ScenarioGenerationSession:
    """One pre-title run with durable author histories and diagnostics.

    Candidate scenario content remains provisional until novelty, OpenAPI, and
    text-spec validation succeed. The candidate is therefore not written as a
    scenario artifact before acceptance, but the author history is persisted
    from the first turn under its generation-run directory. On acceptance, the
    same histories are copied into the scenario's artifact directory.
    """

    root: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    _conversations: dict[str, Conversation] = field(default_factory=dict)

    def conversation(self, name: str, *, system_prompt: str) -> Conversation:
        if name in self._conversations:
            return self._conversations[name]
        path = generation_conversation_path(self.root, self.run_id, name)
        conversation = load_conversation(path)
        if conversation is None:
            conversation = Conversation(
                system_prompt=system_prompt, cache_key=_cache_key(self.run_id, name)
            )
        self._conversations[name] = conversation
        self.persist_conversation(name)
        return conversation

    def persist_conversation(self, name: str) -> str:
        """Write one author history immediately after any state transition."""
        return str(
            persist_conversation(
                generation_conversation_path(self.root, self.run_id, name),
                self._conversations[name],
            )
        )

    def persist_accepted_conversations(self, scenario_root: str) -> None:
        """Copy all durable pre-title author histories into the accepted scenario."""
        for name, conversation in self._conversations.items():
            persist_conversation(
                scenario_generation_conversation_path(scenario_root, name), conversation
            )

    def persist_failure(self, record: ScenarioGenerationFailureRecord) -> str:
        return persist_generation_failure(self.root, self.run_id, record)

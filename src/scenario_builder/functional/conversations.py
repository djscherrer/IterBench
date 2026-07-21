"""Durable implementation-owner threads and cache-friendly judge contexts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from llm import Conversation, Response
from workspace.scenario_builder_paths import implementation_conversation_path

from functional.failure import implementation_digest


def _stable_key(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"scenario-builder:{digest}"


def _implementation_text(implementation: dict) -> str:
    return "\n\n".join(
        f"File {path}:\n```\n{content.strip()}\n```"
        for path, content in implementation.items()
    )


def _implementation_seed_prompt(
    scenario: dict, implementation_key: str, implementation: dict
) -> str:
    return (
        "You are the continuing owner of one generated backend implementation.\n\n"
        f"Your implementation identity is `{implementation_key}`. Keep improving this "
        "exact application when execution feedback arrives. Follow the scenario and "
        "OpenAPI contract, preserve already passing behaviour, and always return "
        "complete files in the requested tagged format.\n\n"
        f"Scenario: {scenario['title']}: {scenario['description']}\n\n"
        f"OpenAPI schema:\n```\n{scenario['schema']}\n```\n\n"
        "The canonical initial implementation is:\n"
        f"{_implementation_text(implementation)}\n"
        "\nConversation artifact lineage:\n"
        "- The scenario contract and canonical initial source are in this turn.\n"
        "- Each later full-file assistant response supersedes the prior source.\n"
        "- Later feedback turns intentionally contain only execution evidence; use "
        "the latest complete source already in this conversation rather than asking "
        "for it to be repeated.\n"
    )


def _conversation_from_payload(payload: dict) -> Conversation | None:
    raw_history = payload.get("history")
    if not isinstance(raw_history, list):
        return None
    conversation = Conversation(
        system_prompt=str(payload.get("system_prompt") or Conversation().system_prompt),
        cache_key=str(payload.get("cache_key") or "") or None,
    )
    for turn in raw_history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        text = turn.get("text")
        if role in {"user", "assistant"} and isinstance(text, str):
            conversation.add_message(
                Response(
                    role=role,
                    text=text,
                    reasoning=str(turn.get("reasoning") or ""),
                )
            )
    return conversation if conversation.responses else None


@dataclass
class ImplementationConversationStore:
    """One append-only repair conversation per generated implementation."""

    root: str
    _sessions: dict[str, Conversation] = field(default_factory=dict)
    _artifact_digests: dict[str, str] = field(default_factory=dict)

    def get(
        self, scenario: dict, implementation_key: str, implementation: dict
    ) -> Conversation:
        cached = self._sessions.get(implementation_key)
        if cached is not None:
            return cached

        path = implementation_conversation_path(self.root, implementation_key)
        conversation: Conversation | None = None
        persisted_digest = ""
        try:
            with open(path, encoding="utf-8") as file:
                payload = json.load(file)
            if payload.get("implementation_key") == implementation_key:
                conversation = _conversation_from_payload(payload)
                persisted_digest = str(payload.get("artifact_digest") or "")
        except (OSError, json.JSONDecodeError, AttributeError):
            conversation = None

        if conversation is None:
            conversation = Conversation(
                cache_key=_stable_key("implementation", scenario["title"], implementation_key)
            )
            conversation.add_message(
                Response(
                    role="user",
                    text=_implementation_seed_prompt(
                        scenario, implementation_key, implementation
                    ),
                )
            )
            conversation.add_message(
                Response(
                    role="assistant",
                    text="I will maintain this implementation and use later execution feedback to revise it.",
                )
            )
        current_digest = implementation_digest(implementation)
        if conversation.responses and persisted_digest and persisted_digest != current_digest:
            # Snapshots are the source of truth. Reconcile a resumed thread
            # before asking it to repair code that changed outside the process.
            conversation.add_message(
                Response(
                    role="user",
                    text=(
                        "The canonical implementation changed outside this conversation. "
                        "Use this replacement source tree from now on:\n\n"
                        + _implementation_text(implementation)
                    ),
                )
            )
            conversation.add_message(
                Response(
                    role="assistant",
                    text="I will treat the supplied source tree as the current canonical implementation.",
                )
            )
        self._sessions[implementation_key] = conversation
        self._artifact_digests[implementation_key] = current_digest
        self.persist(implementation_key)
        return conversation

    def set_implementation(self, implementation_key: str, implementation: dict) -> None:
        """Advance the snapshot digest after the owner returns a valid revision."""
        self._artifact_digests[implementation_key] = implementation_digest(implementation)
        self.persist(implementation_key)

    def persist(self, implementation_key: str) -> None:
        conversation = self._sessions[implementation_key]
        path = implementation_conversation_path(self.root, implementation_key)
        payload = {
            "schema_version": 1,
            "role": "functional_implementation_owner",
            "implementation_key": implementation_key,
            "artifact_digest": self._artifact_digests.get(implementation_key, ""),
            "cache_key": conversation.cache_key,
            "system_prompt": conversation.system_prompt,
            "history": [
                {
                    "role": response.role,
                    "text": response.text,
                    "reasoning": response.reasoning,
                }
                for response in conversation.responses
            ],
        }
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")


def _judge_context(
    scenario: dict,
    *,
    test_header: str,
    test_code: str,
    test_spec: str,
    role: str,
) -> str:
    return (
        f"You are an expert {role} of backend functional tests.\n\n"
        "Treat the scenario and OpenAPI specification as the definitive oracle. "
        "Tests must be sound, deterministic, and free of implementation-specific "
        "assumptions.\n\n"
        f"Scenario: {scenario['title']}: {scenario['description']}\n\n"
        f"OpenAPI schema:\n```\n{scenario['schema']}\n```\n\n"
        f"Shared test header:\n```\n{test_header}\n```\n\n"
        f"Functional test:\n```\n{test_code}\n```\n\n"
        f"Textual test specification:\n{test_spec}\n\n"
        "Respond with one concise evidence-based paragraph and one verdict tag:\n"
        "<VERDICT>1</VERDICT> test is wrong;\n"
        "<VERDICT>2</VERDICT> test is correct;\n"
        "<VERDICT>3</VERDICT> more information is needed;\n"
        "<VERDICT>4</VERDICT> shared header is wrong.\n"
    )


def pair_judge_conversation(
    scenario: dict,
    *,
    test_header: str,
    test_code: str,
    test_spec: str,
) -> Conversation:
    """Fresh, independent pairwise judge with a stable cacheable prefix."""
    context = _judge_context(
        scenario,
        test_header=test_header,
        test_code=test_code,
        test_spec=test_spec,
        role="reviewer",
    )
    return Conversation(
        system_prompt=context,
        cache_key=_stable_key("pair-judge", context),
    )


def aggregate_judge_conversation(
    scenario: dict,
    *,
    test_header: str,
    test_code: str,
    test_spec: str,
) -> Conversation:
    """Fresh test-level aggregate judge with a stable cacheable prefix."""
    context = _judge_context(
        scenario,
        test_header=test_header,
        test_code=test_code,
        test_spec=test_spec,
        role="adjudicator",
    )
    return Conversation(
        system_prompt=context,
        cache_key=_stable_key("aggregate-judge", context),
    )

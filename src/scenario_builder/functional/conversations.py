"""Durable implementation-owner threads and cache-friendly judge contexts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from config import logger

from llm import Conversation, Response
from workspace.scenario_builder_paths import (
    functional_test_suite_conversation_path,
    implementation_conversation_path,
    legacy_functional_implementation_conversation_path,
)

from functional.failure import implementation_digest


def _stable_key(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"scenario-builder:{digest}"


def _implementation_text(implementation: dict) -> str:
    return "\n\n".join(
        f"File {path}:\n```\n{content.strip()}\n```"
        for path, content in implementation.items()
    )


def _implementation_tagged_source(implementation: dict) -> str:
    """Render canonical source in the format required for future repairs."""
    return "\n\n".join(
        f"<{Path(path).name}>\n```\n{content.strip()}\n```\n</{Path(path).name}>"
        for path, content in implementation.items()
    )


def _implementation_seed_prompt(scenario: dict, implementation_key: str) -> str:
    return (
        "You are the backend implementation engineer for this scenario.\n\n"
        f"Your implementation identity is `{implementation_key}`. Implement the complete "
        "application described below. Follow the scenario and OpenAPI contract, and "
        "return every source file in the required tagged format.\n\n"
        f"Scenario: {scenario['title']}: {scenario['description']}\n\n"
        f"OpenAPI schema:\n```\n{scenario['schema']}\n```\n\n"
        "This is an append-only ownership conversation.\n"
        "- Your first response is the canonical initial implementation.\n"
        "- Each later full-file assistant response supersedes the prior source.\n"
        "- Later feedback turns contain execution evidence only; use the latest complete "
        "source already in this conversation rather than asking for it to be repeated.\n"
    )


def _looks_like_legacy_implementation_seed(conversation: Conversation) -> bool:
    """Whether an older artifact placed initial source in the first user turn."""
    if len(conversation.responses) < 2:
        return False
    first, second = conversation.responses[:2]
    return (
        first.role == "user"
        and "The canonical initial implementation is:" in first.text
        and second.role == "assistant"
        and "maintain this implementation" in second.text
    )


def _migrate_legacy_implementation_seed(
    conversation: Conversation,
    scenario: dict,
    implementation_key: str,
    implementation: dict,
) -> Conversation:
    """Rewrite only the bootstrap turns; retain all real repair history."""
    migrated = Conversation(
        system_prompt=conversation.system_prompt,
        cache_key=conversation.cache_key
        or _stable_key("implementation", scenario["title"], implementation_key),
    )
    migrated.add_message(
        Response(
            role="user", text=_implementation_seed_prompt(scenario, implementation_key)
        )
    )
    migrated.add_message(
        Response(role="assistant", text=_implementation_tagged_source(implementation))
    )
    for response in conversation.responses[2:]:
        migrated.add_message(response)
    return migrated


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

    def _read_persisted(
        self, path: str, implementation_key: str
    ) -> tuple[Conversation | None, str]:
        try:
            with open(path, encoding="utf-8") as file:
                payload = json.load(file)
            if payload.get("implementation_key") == implementation_key:
                return _conversation_from_payload(payload), str(
                    payload.get("artifact_digest") or ""
                )
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return None, ""

    def get(
        self, scenario: dict, implementation_key: str, implementation: dict
    ) -> Conversation:
        cached = self._sessions.get(implementation_key)
        if cached is not None:
            self._reconcile_cached_implementation(implementation_key, implementation)
            return cached

        conversation, persisted_digest = self._read_persisted(
            implementation_conversation_path(self.root, implementation_key),
            implementation_key,
        )
        if conversation is None:
            conversation, persisted_digest = self._read_persisted(
                legacy_functional_implementation_conversation_path(
                    self.root, implementation_key
                ),
                implementation_key,
            )
            if conversation is not None:
                logger.info(
                    "Migrating implementation conversation for %s from the "
                    "functional-phase path to %s",
                    implementation_key,
                    implementation_conversation_path(self.root, implementation_key),
                )

        if conversation is None:
            conversation = Conversation(
                cache_key=_stable_key(
                    "implementation", scenario["title"], implementation_key
                )
            )
            conversation.add_message(
                Response(
                    role="user",
                    text=_implementation_seed_prompt(scenario, implementation_key),
                )
            )
            conversation.add_message(
                Response(
                    role="assistant",
                    text=_implementation_tagged_source(implementation),
                )
            )
        elif _looks_like_legacy_implementation_seed(conversation):
            conversation = _migrate_legacy_implementation_seed(
                conversation, scenario, implementation_key, implementation
            )
        current_digest = implementation_digest(implementation)
        self._sessions[implementation_key] = conversation
        self._artifact_digests[implementation_key] = persisted_digest or current_digest
        self._reconcile_cached_implementation(implementation_key, implementation)
        if self._artifact_digests[implementation_key] == current_digest:
            self.persist(implementation_key)
        return conversation

    def _reconcile_cached_implementation(
        self, implementation_key: str, implementation: dict
    ) -> None:
        """Make an owner thread follow the source snapshot used for a repair."""
        current_digest = implementation_digest(implementation)
        persisted_digest = self._artifact_digests.get(implementation_key, "")
        if persisted_digest and persisted_digest != current_digest:
            conversation = self._sessions[implementation_key]
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
        self._artifact_digests[implementation_key] = current_digest
        self.persist(implementation_key)

    def set_implementation(self, implementation_key: str, implementation: dict) -> None:
        """Advance the snapshot digest after the owner returns a valid revision."""
        self._artifact_digests[implementation_key] = implementation_digest(
            implementation
        )
        self.persist(implementation_key)

    def persist(self, implementation_key: str) -> None:
        conversation = self._sessions[implementation_key]
        path = implementation_conversation_path(self.root, implementation_key)
        payload = {
            "schema_version": 2,
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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")


def functional_suite_digest(scenario: dict) -> str:
    """Stable digest for the header, specifications, and test functions in a suite."""
    state = {
        "header": scenario.get("header_code", ""),
        "tests": [
            {
                "name": name,
                "specification": specification,
                "code": code,
            }
            for name, specification, code in zip(
                scenario.get("functional_tests_names", []),
                scenario.get("tests_spec", []),
                scenario.get("functional_tests_code", []),
                strict=True,
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _suite_snapshot(scenario: dict) -> str:
    tests = []
    for name, specification, code in zip(
        scenario["functional_tests_names"],
        scenario["tests_spec"],
        scenario["functional_tests_code"],
        strict=True,
    ):
        tests.append(
            f"### Test `{name}`\n"
            f"Textual specification:\n{specification}\n\n"
            f"Implementation:\n```python\n{code.strip()}\n```"
        )
    return (
        "## Canonical functional test suite\n\n"
        f"Shared header:\n```python\n{scenario['header_code'].strip()}\n```\n\n"
        + "\n\n".join(tests)
    )


def _suite_seed_prompt(scenario: dict) -> str:
    return (
        "You are the continuing author of one backend functional test suite.\n\n"
        "Create deterministic tests that exercise only behaviour defined by the "
        "scenario contract. Later reviewer feedback may require you to revise one "
        "test or the shared header; preserve unrelated tests unless evidence requires "
        "otherwise.\n\n"
        f"Scenario: {scenario['title']}: {scenario['description']}\n\n"
        f"OpenAPI schema:\n```\n{scenario['schema']}\n```\n"
    )


@dataclass
class FunctionalTestSuiteConversationStore:
    """One append-only author conversation for a scenario's functional suite."""

    root: str
    _conversation: Conversation | None = None
    _artifact_digest: str = ""

    def begin_initial_generation(self, scenario: dict) -> Conversation:
        """Persist the initial suite-author thread before its first model call."""
        self._conversation = Conversation(
            cache_key=_stable_key("functional-test-suite", scenario["title"])
        )
        self._artifact_digest = ""
        self.persist()
        return self._conversation

    def get(self, scenario: dict) -> Conversation:
        current_digest = functional_suite_digest(scenario)
        if self._conversation is None:
            path = functional_test_suite_conversation_path(self.root)
            try:
                with open(path, encoding="utf-8") as file:
                    payload = json.load(file)
                if payload.get("role") == "functional_test_suite_author":
                    self._conversation = _conversation_from_payload(payload)
                    self._artifact_digest = str(payload.get("artifact_digest") or "")
            except (OSError, json.JSONDecodeError, AttributeError):
                self._conversation = None
            if self._conversation is None:
                self._conversation = Conversation(
                    cache_key=_stable_key("functional-test-suite", scenario["title"])
                )
                self._conversation.add_message(
                    Response(role="user", text=_suite_seed_prompt(scenario))
                )
                self._conversation.add_message(
                    Response(role="assistant", text=_suite_snapshot(scenario))
                )
                self._artifact_digest = current_digest
                self.persist()

        self._reconcile(scenario, current_digest)
        return self._conversation

    def adopt_initial_generation(
        self, conversation: Conversation, scenario: dict
    ) -> Conversation:
        """Persist the real initial test-generation exchange as the suite history."""
        if not conversation.cache_key:
            conversation.cache_key = _stable_key(
                "functional-test-suite", scenario["title"]
            )
        self._conversation = conversation
        self._artifact_digest = functional_suite_digest(scenario)
        self.persist()
        return conversation

    def set_suite(self, scenario: dict) -> None:
        """Advance the canonical suite digest after this owner produced a revision."""
        if self._conversation is None:
            self.get(scenario)
        self._artifact_digest = functional_suite_digest(scenario)
        self.persist()

    def _reconcile(self, scenario: dict, current_digest: str) -> None:
        assert self._conversation is not None
        if self._artifact_digest and self._artifact_digest != current_digest:
            self._conversation.add_message(
                Response(
                    role="user",
                    text=(
                        "The canonical functional test suite changed outside this "
                        "conversation. Treat this snapshot as the current suite:\n\n"
                        + _suite_snapshot(scenario)
                    ),
                )
            )
            self._conversation.add_message(
                Response(
                    role="assistant",
                    text="I will treat the supplied suite snapshot as the current canonical version.",
                )
            )
            self._artifact_digest = current_digest
            self.persist()

    def persist(self) -> None:
        assert self._conversation is not None
        path = functional_test_suite_conversation_path(self.root)
        payload = {
            "schema_version": 1,
            "role": "functional_test_suite_author",
            "artifact_digest": self._artifact_digest,
            "cache_key": self._conversation.cache_key,
            "system_prompt": self._conversation.system_prompt,
            "history": [
                {
                    "role": response.role,
                    "text": response.text,
                    "reasoning": response.reasoning,
                }
                for response in self._conversation.responses
            ],
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
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

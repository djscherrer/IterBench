"""Persistence helpers for scenario-builder ``llm.Conversation`` objects."""

from __future__ import annotations

import json
from pathlib import Path

from llm import Conversation, Response


def load_conversation(path: str | Path) -> Conversation | None:
    """Load a persisted conversation, ignoring incomplete/corrupt artifacts."""
    source = Path(path)
    if not source.is_file():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        return None

    conversation = Conversation(
        system_prompt=str(payload.get("system_prompt") or Conversation().system_prompt),
        cache_key=str(payload.get("cache_key") or "") or None,
    )
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role, text = turn.get("role"), turn.get("text")
        if role in {"user", "assistant"} and isinstance(text, str):
            conversation.add_message(
                Response(
                    role=role,
                    text=text,
                    reasoning=str(turn.get("reasoning") or ""),
                )
            )
    return conversation if conversation.responses else None


def persist_conversation(path: str | Path, conversation: Conversation) -> Path:
    """Persist complete history; provider response usage is intentionally omitted."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
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
    output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return output

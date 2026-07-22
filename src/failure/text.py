"""Text trimming and prompt-safe diagnostic helpers shared by failure domains."""

from __future__ import annotations

import re

_ORACLE_HINT_RE = re.compile(r"\bexpected\b", re.IGNORECASE)


def sanitize_test_log_tail(text: str) -> str:
    """Remove assertion-oracle lines before sending a test log to an author."""
    if not text:
        return ""
    return "\n".join(
        line for line in text.splitlines() if not _ORACLE_HINT_RE.search(line)
    ).strip()


def trim(text: str, *, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…(truncated)"


def tail(text: str, *, max_lines: int, max_chars: int = 1600) -> str:
    return trim("\n".join((text or "").splitlines()[-max_lines:]), max_chars=max_chars)


def failure_prompt_header(
    *,
    stage_label: str,
    iteration_id: str,
    attempt: int | None,
    kind: str,
) -> list[str]:
    attempt_label = f"attempt {attempt}" if attempt is not None else iteration_id
    return [
        f"**{stage_label} (`{attempt_label}`) failed.**",
        "",
        f"- **Kind**: `{kind}`",
        "",
    ]

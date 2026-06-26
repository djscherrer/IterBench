"""Small text helpers for functional-test log excerpts."""

from __future__ import annotations

import re

# BaxBench scenarios log assertions as ``... mismatch. Expected <X>, got <Y>``;
# drop oracle lines so the coding agent cannot hardcode outputs.
_ORACLE_HINT_RE = re.compile(r"\bexpected\b", re.IGNORECASE)


def sanitize_test_log_tail(text: str) -> str:
    """Strip oracle-revealing lines from a per-test harness log tail."""
    if not text:
        return ""
    kept = [ln for ln in text.splitlines() if not _ORACLE_HINT_RE.search(ln)]
    return "\n".join(kept).strip()


def trim(text: str, *, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…(truncated)"


def tail(text: str, *, max_lines: int, max_chars: int = 1600) -> str:
    lines = (text or "").splitlines()
    excerpt = "\n".join(lines[-max_lines:])
    return trim(excerpt, max_chars=max_chars)

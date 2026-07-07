"""Small text helpers for functional-test log excerpts."""

from __future__ import annotations

import re

# BaxBench scenarios log assertions as ``... mismatch. Expected <X>, got <Y>``;
# drop oracle lines so the coding agent cannot hardcode outputs.
_ORACLE_HINT_RE = re.compile(r"\bexpected\b", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_COMPILE_DIAGNOSTIC_LINE_RE = re.compile(
    r"(error\[E\d+\]|^error:|warning:|could not compile|"
    r"Some errors have|For more information|rustc --|npm ERR!|"
    r"^\s*-->|^\s*\d+\s*\||^\s*\||^\s*\^|^\s*=\s*(note|help):)",
    re.IGNORECASE,
)
_HARNESS_NOISE_LINE_RE = re.compile(
    r"(^File \"/|^Traceback \(most recent call last\)|"
    r"docker\.errors\.BuildError|Failed to build docker image.*got exception|"
    r"During handling of the above exception|"
    r"^The command '/bin/sh -c cargo build'|Removed intermediate container)",
    re.IGNORECASE,
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def strip_harness_noise(text: str) -> str:
    """Drop Python tracebacks and BaxBench harness lines from a log excerpt."""
    if not text:
        return ""
    cut = text.split("Traceback (most recent call last)")[0]
    kept: list[str] = []
    for line in cut.splitlines():
        clean = strip_ansi(line).strip()
        if not clean:
            continue
        if _HARNESS_NOISE_LINE_RE.search(clean):
            continue
        kept.append(clean)
    return "\n".join(kept).strip()


def filter_compile_diagnostics(text: str, *, max_lines: int = 40) -> str:
    """Keep only compiler warnings/errors from docker/cargo build output."""
    kept = [
        ln
        for ln in strip_harness_noise(text).splitlines()
        if _COMPILE_DIAGNOSTIC_LINE_RE.search(ln)
    ]
    return "\n".join(kept[-max_lines:]).strip()


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


def failure_prompt_header(
    *,
    stage_label: str,
    iteration_id: str,
    attempt: int | None,
    kind: str,
) -> list[str]:
    """Shared opening lines for ``FailureRecord.to_prompt_block()``."""
    attempt_label = f"attempt {attempt}" if attempt is not None else iteration_id
    return [
        f"**{stage_label} (`{attempt_label}`) failed.**",
        "",
        f"- **Kind**: `{kind}`",
        "",
    ]


def tail(text: str, *, max_lines: int, max_chars: int = 1600) -> str:
    lines = (text or "").splitlines()
    excerpt = "\n".join(lines[-max_lines:])
    return trim(excerpt, max_chars=max_chars)

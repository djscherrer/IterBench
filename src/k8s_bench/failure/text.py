"""K8s-specific text helpers plus compatibility re-exports from ``failure``."""

from __future__ import annotations

import re

from failure.text import failure_prompt_header, sanitize_test_log_tail, tail, trim

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

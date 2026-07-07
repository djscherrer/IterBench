"""Collect bench harness log excerpts for bench failure triage."""

from __future__ import annotations

from pathlib import Path

from ..workspace import iteration_bench_log_path
from .text import trim


def collect_bench_failure_diagnostics(
    iteration_path: Path,
    *,
    tail_lines: int = 120,
    max_chars: int = 8000,
) -> str:
    """
    Tail ``05-bench/bench.log`` for ``BenchFailureRecord.diagnostic_excerpt``.
    """
    bench_log = iteration_bench_log_path(iteration_path)
    if not bench_log.is_file():
        return ""
    try:
        text = bench_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.strip():
        return ""
    tail = "\n".join(text.splitlines()[-tail_lines:])
    return trim(
        f"### {bench_log.name} (tail)\n```\n{tail.strip()}\n```",
        max_chars=max_chars,
    )

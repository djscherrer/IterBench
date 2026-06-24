"""Read and render multi-file application code snapshots for LLM prompts."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_CODE_BUDGET_CHARS = 200_000

_CODE_IGNORE_NAMES = frozenset({
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".cache",
    ".idea",
    ".vscode",
})
_CODE_IGNORE_SUFFIXES = frozenset({
    ".lock",
    ".log",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".class",
    ".jar",
    ".zip",
    ".tar",
    ".gz",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
    ".sqlite",
    ".db",
})


def iter_code_files(code_dir: Path) -> list[Path]:
    if not code_dir.is_dir():
        return []
    priority_first = ("app.js", "app.py", "main.py", "main.rs", "server.js", "index.js")
    found: list[Path] = []
    for p in code_dir.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _CODE_IGNORE_NAMES for part in p.relative_to(code_dir).parts):
            continue
        if p.suffix.lower() in _CODE_IGNORE_SUFFIXES:
            continue
        found.append(p)

    def _sort_key(path: Path) -> tuple[int, str]:
        name = path.name
        try:
            return (priority_first.index(name), str(path))
        except ValueError:
            return (len(priority_first), str(path))

    return sorted(found, key=_sort_key)


def render_code_files(code_dir: Path, *, budget_chars: int) -> str:
    files = iter_code_files(code_dir)
    if not files:
        return "(application code not found yet)"

    blocks: list[str] = []
    skipped: list[str] = []
    used = 0
    for path in files:
        rel = path.relative_to(code_dir).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append(f"{rel} (unreadable)")
            continue
        block = f"<FILEPATH>\n{rel}\n</FILEPATH>\n<CODE>\n{content}\n</CODE>\n"
        if used + len(block) > budget_chars and blocks:
            skipped.append(rel)
            continue
        blocks.append(block)
        used += len(block)

    body = "\n".join(blocks).rstrip()
    if skipped:
        body += (
            "\n\n(Additional files in the codebase, not shown due to context "
            "budget — assume they exist unchanged: "
            + ", ".join(skipped)
            + ")"
        )
    return body


def read_full_code_for_refinement(
    code_dir: Path, *, budget_chars: int | None = None
) -> str:
    if budget_chars is None:
        budget_chars = int(
            os.environ.get(
                "BAXBENCH_K8S_CODE_REFINE_MAX_CHARS",
                str(_DEFAULT_CODE_BUDGET_CHARS),
            )
        )
    return render_code_files(code_dir, budget_chars=budget_chars)

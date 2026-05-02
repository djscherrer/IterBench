#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Hit:
    test_log: Path
    functional_tests_dir: Path
    first_error_line: str


def _first_error_line(p: Path) -> str | None:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Your logs look like: "ERROR 2026-... ..."
                if line.startswith("ERROR"):
                    return line.rstrip("\n")
    except FileNotFoundError:
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Find functional test runs whose test.log contains a line starting with 'ERROR'. "
            "Dry-run by default; pass --delete to remove the functional_tests directory."
        )
    )
    ap.add_argument(
        "--results-root",
        type=Path,
        default=Path("results"),
        help="Root directory to scan (default: ./results).",
    )
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete matched functional_tests/ directories.",
    )
    args = ap.parse_args()

    root: Path = args.results_root
    logs = sorted(root.glob("**/functional_tests/test.log"))
    if not logs:
        raise SystemExit(f"No functional test logs found under: {root}")

    hits: list[Hit] = []
    for test_log in logs:
        err = _first_error_line(test_log)
        if err is None:
            continue
        hits.append(
            Hit(
                test_log=test_log,
                functional_tests_dir=test_log.parent,
                first_error_line=err,
            )
        )

    if not hits:
        print("No functional test logs contained 'ERROR' at line start.")
        return

    try:
        print(f"Found {len(hits)} functional_tests/test.log with ERROR:")
        for h in hits:
            print(f"- {h.test_log}")
            print(f"  {h.first_error_line}")
    except BrokenPipeError:
        # Allow piping to `head` without stacktraces.
        return

    if not args.delete:
        print("\nDry-run only. Re-run with --delete to remove the directories above.")
        return

    deleted = 0
    for h in hits:
        try:
            if h.functional_tests_dir.exists():
                # Remove the whole dir so the pipeline can regenerate cleanly.
                for child in sorted(h.functional_tests_dir.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                h.functional_tests_dir.rmdir()
                deleted += 1
        except Exception as e:
            print(f"[WARN] Failed to delete {h.functional_tests_dir}: {e}")

    print(f"\nDeleted {deleted} functional_tests/ director(ies).")


if __name__ == "__main__":
    main()


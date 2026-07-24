#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PerfRun:
    bench_log: Path
    run_dir: Path
    finished: bool
    has_error_line: bool


def _bench_finished(bench_log_text: str) -> bool:
    return "finished benchmarking sample" in bench_log_text.lower()


def _has_error_line(text: str) -> bool:
    # Bench logs typically prefix with INFO/WARN/ERROR.
    return "\nERROR " in ("\n" + text) or text.startswith("ERROR ")


def _rm_tree(p: Path) -> None:
    # Avoid shutil.rmtree (sometimes slow on large NFS dirs). Do a simple walk.
    for child in sorted(p.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            try:
                child.rmdir()
            except OSError:
                # Directory not empty; keep going.
                pass
    # Best-effort remove remaining empty dirs bottom-up.
    for child in sorted([d for d in p.rglob("*") if d.is_dir()], reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass
    p.rmdir()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Find and optionally delete perf run directories (perf-*/...) based on bench.log. "
            "A run is considered 'finished' if bench.log contains 'finished benchmarking sample'."
        )
    )
    ap.add_argument(
        "--results-root",
        type=Path,
        default=Path("results"),
        help="Root directory to scan (default: ./results).",
    )
    ap.add_argument(
        "--only-incomplete",
        action="store_true",
        help="Only match runs missing the 'finished benchmarking sample' marker.",
    )
    ap.add_argument(
        "--only-error",
        action="store_true",
        help="Only match runs that contain an ERROR line.",
    )
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete matched perf run directories (otherwise dry-run).",
    )
    args = ap.parse_args()

    root: Path = args.results_root
    bench_logs = sorted(root.glob("**/perf-*/bench.log"))
    if not bench_logs:
        raise SystemExit(f"No perf bench logs found under: {root}")

    runs: list[PerfRun] = []
    for bench_log in bench_logs:
        try:
            text = bench_log.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        finished = _bench_finished(text)
        has_error = _has_error_line(text)
        runs.append(
            PerfRun(
                bench_log=bench_log,
                run_dir=bench_log.parent,
                finished=finished,
                has_error_line=has_error,
            )
        )

    matched: list[PerfRun] = []
    for r in runs:
        if args.only_incomplete and r.finished:
            continue
        if args.only_error and not r.has_error_line:
            continue
        matched.append(r)

    if not matched:
        print(
            "No perf runs matched. "
            f"(total_runs={len(runs)} only_incomplete={args.only_incomplete} only_error={args.only_error})"
        )
        return

    print(
        f"Matched {len(matched)} perf run(s) (total_runs={len(runs)}):"
    )
    for r in matched[:200]:
        status = "finished" if r.finished else "INCOMPLETE"
        err = " error" if r.has_error_line else ""
        print(f"- {r.run_dir}  ({status}{err})")

    if len(matched) > 200:
        print(f"... ({len(matched)-200} more not shown)")

    if not args.delete:
        print("\nDry-run only. Re-run with --delete to remove the directories above.")
        return

    deleted = 0
    for r in matched:
        try:
            if r.run_dir.exists():
                _rm_tree(r.run_dir)
                deleted += 1
        except Exception as e:
            print(f"[WARN] Failed to delete {r.run_dir}: {e}")

    print(f"\nDeleted {deleted} perf run director(ies).")


if __name__ == "__main__":
    main()


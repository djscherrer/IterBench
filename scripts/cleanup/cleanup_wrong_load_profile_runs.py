#!/usr/bin/env python3
"""
Delete the deploy+bench artifacts of iterations whose bench.log records a
specific benchmark error, so they can be re-benchmarked cleanly.

Motivating case: a re-bench sweep was launched with a mistyped
``--load-profile`` (``explore-refine`` instead of ``k8s-explore-refine``).
Every affected iteration crashed immediately with

    ERROR ... k8s bench failed: Unknown load profile 'explore-refine'.

producing an ``05-bench/bench.log`` that holds only that traceback (no real
measurement) and getting its folder renamed to ``...-failed``. This tool
finds exactly those iterations by matching the error text in ``bench.log``,
then removes ``04-deploy/``, ``05-bench/`` and ``iteration.log`` for each so a
subsequent ``k8s_rebench_results.py --only-missing-artifacts`` run re-does
only them.

By default it also strips the ``-failed`` suffix that the crash added
(``--keep-failed-suffix`` to leave it): the crash was an invocation mistake,
not a real stage failure, and ``--only-missing-artifacts`` discovery skips
``-failed`` folders, so the rename is what makes them re-benchable again. A
rename is skipped (with a warning) if the un-suffixed folder already exists.

Dry-run by default; pass ``--apply`` to actually delete/rename. Only ever
touches paths under ``--results-dir``.

    # see what would happen
    python scripts/cleanup/cleanup_wrong_load_profile_runs.py \
        --results-dir results_reverified

    # do it, and write the affected list to a file
    python scripts/cleanup/cleanup_wrong_load_profile_runs.py \
        --results-dir results_reverified --apply --list-out affected.txt
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_DEFAULT_ERROR = "Unknown load profile 'explore-refine'"


def _iter_bench_logs(results_dir: Path):
    yield from results_dir.rglob("05-bench/bench.log")


def _matches(bench_log: Path, needle: str) -> bool:
    try:
        return needle in bench_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _assert_within(target: Path, root: Path) -> Path:
    t = target.resolve()
    r = root.resolve()
    if t != r and r not in t.parents:
        raise SystemExit(f"refusing to touch {t}: outside results dir {r}")
    return t


def _unsuffixed_name(name: str) -> str | None:
    """Return the folder name with a trailing '-failed' removed, or None."""
    return name[: -len("-failed")] if name.endswith("-failed") else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--results-dir", required=True, type=Path,
                    help="Results tree to scan (e.g. results_reverified).")
    ap.add_argument("--error-string", default=_DEFAULT_ERROR,
                    help=f"Substring to match in bench.log (default: {_DEFAULT_ERROR!r}).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete/rename. Without this it is a dry run.")
    ap.add_argument("--keep-failed-suffix", action="store_true",
                    help="Do NOT strip the '-failed' suffix from affected iteration folders.")
    ap.add_argument("--list-out", type=Path, default=None,
                    help="Write the affected iteration paths (one per line) to this file.")
    args = ap.parse_args(argv)

    root = args.results_dir.expanduser()
    if not root.is_dir():
        print(f"--results-dir not found: {root}", file=sys.stderr)
        return 2

    affected: list[Path] = []
    for bench_log in _iter_bench_logs(root):
        if _matches(bench_log, args.error_string):
            affected.append(bench_log.parent.parent)  # 05-bench/bench.log -> iteration dir
    affected = sorted(set(affected))

    if not affected:
        print(f"No iterations matched {args.error_string!r} under {root}.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(affected)} iteration(s) matched {args.error_string!r}:\n")
    for it in affected:
        rel = it.relative_to(root)
        targets = [it / "04-deploy", it / "05-bench", it / "iteration.log"]
        present = [t for t in targets if t.exists()]
        new_name = None if args.keep_failed_suffix else _unsuffixed_name(it.name)
        rename_note = ""
        if new_name:
            dest = it.with_name(new_name)
            if dest.exists():
                rename_note = f"  (rename skipped: {new_name} already exists)"
                new_name = None
            else:
                rename_note = f"  -> rename to {new_name}"
        print(f"  {rel}{rename_note}")
        for t in present:
            print(f"      delete {t.relative_to(root)}")

        if args.apply:
            for t in present:
                _assert_within(t, root)
                if t.is_dir():
                    shutil.rmtree(t)
                else:
                    t.unlink(missing_ok=True)
            if new_name:
                _assert_within(it, root)
                it.rename(it.with_name(new_name))

    if args.list_out:
        args.list_out.write_text(
            "\n".join(str(it.relative_to(root)) for it in affected) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote affected list to {args.list_out}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to delete/rename.")
    else:
        print(f"\nDone. Removed deploy/bench for {len(affected)} iteration(s).")
        print("Re-bench them with:")
        print(f"  python scripts/k8s_rebench_results.py --results-dir {root} \\")
        print("    --cluster baxbench-emulab --load-profile k8s-explore-refine \\")
        print("    --only-missing-artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

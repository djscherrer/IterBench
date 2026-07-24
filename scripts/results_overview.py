#!/usr/bin/env python3
"""
Summarize BaxBench ``results/`` in the usual layout:

  results/<model>/<scenario>/<framework>/<variant>/sampleN/

Stages per sample:
  GEN   — ``code/`` must exist (**missing** if absent). For known frameworks, **ok** only
          if the primary source file exists (Flask: ``app.py``; Express: ``app.js``;
          Actix: ``main.rs``). Manifests are not required for this label.
  TEST  — ``functional_tests/`` + ``test_results.json`` + ``test.log`` (ERROR lines).
  BENCH — ``perf-*/bench.log`` (+ optional Locust ``*_stats.csv`` failure ratio).

Default: write a plain-text tree to ``./results_summary.txt`` (override with ``--out-txt``).
Use ``--print`` for colored terminal output. ``--out-json`` writes a structured JSON snapshot.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from env import all_envs  # noqa: E402

_SAMPLE_RE = re.compile(r"^sample(\d+)$")

_ENV_BY_ID = {e.id: e for e in all_envs}


# ---------------------------------------------------------------------------
# Sample scan + classification
# ---------------------------------------------------------------------------


def _primary_source_fallback_path(env_id: str) -> Path | None:
    """Single required file under ``code/`` when ``env_id`` is not in ``all_envs``."""
    env_l = env_id.lower()
    if "python" in env_l and "flask" in env_l:
        return Path("app.py")
    if "javascript" in env_l and "express" in env_l:
        return Path("app.js")
    if "rust" in env_l and "actix" in env_l:
        return Path("main.rs")
    if "go" in env_l and ("net-http" in env_l or "net/http" in env_l or "http" in env_l):
        return Path("main.go")
    return None


def _looks_generated_fallback(sample_dir: Path, env_id: str) -> bool:
    code_dir = sample_dir / "code"
    if not code_dir.is_dir():
        return False
    rel = _primary_source_fallback_path(env_id)
    if rel is not None:
        return (code_dir / rel).is_file()
    return any(p.is_file() for p in code_dir.iterdir())


def _parse_sample_dir(sample_dir: Path, results_root: Path) -> tuple[str, str, str, str, int]:
    rel = sample_dir.relative_to(results_root)
    parts = rel.parts
    if len(parts) < 5:
        raise ValueError(f"Unexpected sample dir layout: {sample_dir}")
    model, scenario, env, temp_dir, sample_part = (
        parts[0],
        parts[1],
        parts[2],
        parts[3],
        parts[4],
    )
    m = _SAMPLE_RE.match(sample_part)
    if not m:
        raise ValueError(f"Unexpected sample dir name: {sample_part}")
    return model, scenario, env, temp_dir, int(m.group(1))


def _file_has_error_line_starting_with_error(p: Path) -> bool | None:
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("ERROR"):
                    return True
    except OSError:
        return None
    return False


def _bench_log_finished(bench_log: Path) -> bool:
    try:
        txt = bench_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "finished benchmarking sample" in txt.lower()


def _load_test_results_json(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _locust_aggregate_failure_ratio(run_dir: Path) -> float | None:
    """
    Best-effort: read Locust ``locust/results/<test>_stats.csv`` Aggregated row.
    Returns failure_count / request_count, or None if not computable.
    """
    for stats in sorted((run_dir / "locust" / "results").glob("*_stats.csv")):
        try:
            with stats.open("r", encoding="utf-8", errors="replace", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    name = (row.get("Name") or "").strip()
                    if name != "Aggregated":
                        continue
                    try:
                        req = int(float(row.get("Request Count") or "0"))
                        fail = int(float(row.get("Failure Count") or "0"))
                    except (TypeError, ValueError):
                        continue
                    if req <= 0:
                        return None
                    return fail / req
        except OSError:
            continue
    return None


def _perf_run_ok(run_dir: Path, *, max_failure_ratio: float) -> bool:
    bench_log = run_dir / "bench.log"
    if not bench_log.is_file():
        return False
    if _file_has_error_line_starting_with_error(bench_log):
        return False
    if not _bench_log_finished(bench_log):
        return False
    ratio = _locust_aggregate_failure_ratio(run_dir)
    if ratio is not None and ratio > max_failure_ratio:
        return False
    return True


def classify_gen(sample_dir: Path, env_id: str) -> str:
    code_dir = sample_dir / "code"
    if not code_dir.exists():
        return "missing"
    if not code_dir.is_dir():
        return "fail"
    env = _ENV_BY_ID.get(env_id)
    if env is not None:
        errs = env.codegen_layout_errors(code_dir)
        return "ok" if not errs else "fail"
    return "ok" if _looks_generated_fallback(sample_dir, env_id) else "fail"


def classify_test(sample_dir: Path) -> str:
    ft_dir = sample_dir / "functional_tests"
    if not ft_dir.is_dir():
        return "missing"
    tr_path = ft_dir / "test_results.json"
    log_path = ft_dir / "test.log"
    err = _file_has_error_line_starting_with_error(log_path)
    if err is True:
        return "fail"
    data = _load_test_results_json(tr_path)
    if data is None:
        return "missing"
    try:
        passed = int(data.get("num_passed_ft", 0))
        total = int(data.get("num_total_ft", 0))
    except (TypeError, ValueError):
        return "missing"
    if total <= 0:
        # Ran tests but recorded nothing useful — treat as missing signal.
        return "missing"
    if passed < total:
        return "fail"
    if err is False or err is None:
        # No ERROR line in log (or no log): still ok if JSON says all passed.
        return "ok"
    return "ok"


def classify_bench(sample_dir: Path, *, max_failure_ratio: float) -> str:
    perf_dirs = [p for p in sample_dir.glob("perf-*") if p.is_dir()]
    if not perf_dirs:
        return "missing"
    any_log = False
    for d in perf_dirs:
        if (d / "bench.log").is_file():
            any_log = True
            if _perf_run_ok(d, max_failure_ratio=max_failure_ratio):
                return "ok"
    if not any_log:
        return "missing"
    return "fail"


@dataclass
class SampleRow:
    sample_dir: Path
    model: str
    scenario: str
    env: str
    variant: str
    sample: int
    gen: str
    test: str
    bench: str


def collect_samples(results_root: Path, *, max_failure_ratio: float) -> list[SampleRow]:
    rows: list[SampleRow] = []
    for sample_dir in sorted(results_root.glob("**/sample*")):
        if not sample_dir.is_dir():
            continue
        if not _SAMPLE_RE.match(sample_dir.name):
            continue
        try:
            model, scenario, env, variant, snum = _parse_sample_dir(sample_dir, results_root)
        except ValueError:
            continue
        rows.append(
            SampleRow(
                sample_dir=sample_dir,
                model=model,
                scenario=scenario,
                env=env,
                variant=variant,
                sample=snum,
                gen=classify_gen(sample_dir, env),
                test=classify_test(sample_dir),
                bench=classify_bench(sample_dir, max_failure_ratio=max_failure_ratio),
            )
        )
    return rows


def counts_for_statuses(statuses: Iterable[str]) -> dict[str, int]:
    c = {"ok": 0, "fail": 0, "missing": 0}
    for s in statuses:
        if s in c:
            c[s] += 1
    return c


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _ansi(code: str, s: str, *, enabled: bool) -> str:
    if not enabled:
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def _fmt_stage(
    label: str,
    counts: dict[str, int],
    *,
    ansi: bool,
    num_w: int = 3,
) -> str:
    """One stage: ``GEN ok=  5  fail=  0  missing=  0`` with colors when ``ansi``."""
    ok = counts.get("ok", 0)
    fail = counts.get("fail", 0)
    miss = counts.get("missing", 0)
    ok_s = _ansi("1;32", f"{label} ok={ok:>{num_w}}", enabled=ansi)
    fail_s = _ansi("1;31", f"fail={fail:>{num_w}}", enabled=ansi)
    miss_s = _ansi("38;5;208", f"missing={miss:>{num_w}}", enabled=ansi)
    return f"{ok_s}  {fail_s}  {miss_s}"


def _fmt_variant_line(
    gen_c: dict[str, int],
    test_c: dict[str, int],
    bench_c: dict[str, int],
    *,
    ansi: bool,
) -> str:
    g = _fmt_stage("GEN", gen_c, ansi=ansi)
    t = _fmt_stage("TEST", test_c, ansi=ansi)
    b = _fmt_stage("BENCH", bench_c, ansi=ansi)
    return f"{g}    {t}    {b}"


def _compute_totals(rows: list[SampleRow]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    gen = counts_for_statuses(r.gen for r in rows)
    tst = counts_for_statuses(r.test for r in rows)
    ben = counts_for_statuses(r.bench for r in rows)
    return gen, tst, ben


def build_overview_tree(rows: list[SampleRow]) -> dict[str, Any]:
    """Nested dict suitable for JSON export."""
    tree: dict[str, Any] = {}
    for r in rows:
        tree.setdefault(r.model, {}).setdefault(r.scenario, {}).setdefault(r.env, {}).setdefault(
            r.variant, []
        ).append(
            {
                "sample": r.sample,
                "path": str(r.sample_dir),
                "GEN": r.gen,
                "TEST": r.test,
                "BENCH": r.bench,
            }
        )
    return tree


def write_text_tree(
    path: Path,
    rows: list[SampleRow],
    *,
    max_failure_ratio: float,
    generated_at: str,
) -> None:
    lines: list[str] = []
    lines.append(f"# BaxBench results overview (generated {generated_at})")
    lines.append(f"# bench failure ratio threshold: locust failures/requests > {max_failure_ratio} => BENCH fail")
    lines.append("")

    tree: dict[str, dict[str, dict[str, dict[str, list[SampleRow]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    for r in rows:
        tree[r.model][r.scenario][r.env][r.variant].append(r)

    for model in sorted(tree.keys()):
        lines.append(model)
        for scenario in sorted(tree[model].keys()):
            lines.append(f"  {scenario}")
            scen_gen: list[str] = []
            scen_tst: list[str] = []
            scen_ben: list[str] = []
            for env in sorted(tree[model][scenario].keys()):
                lines.append(f"    {env}")
                for variant in sorted(tree[model][scenario][env].keys()):
                    vrows = tree[model][scenario][env][variant]
                    gen_c = counts_for_statuses(x.gen for x in vrows)
                    tst_c = counts_for_statuses(x.test for x in vrows)
                    ben_c = counts_for_statuses(x.bench for x in vrows)
                    scen_gen.extend(x.gen for x in vrows)
                    scen_tst.extend(x.test for x in vrows)
                    scen_ben.extend(x.bench for x in vrows)
                    oh = "openhands" if "openhands" in variant.lower() else "single-shot"
                    lines.append(f"      {variant} ({oh})  n={len(vrows)}")
                    lines.append(
                        "        "
                        + _fmt_variant_line(gen_c, tst_c, ben_c, ansi=False)
                    )
            lines.append(
                "    "
                + _fmt_stage("GEN", counts_for_statuses(scen_gen), ansi=False)
                + "    "
                + _fmt_stage("TEST", counts_for_statuses(scen_tst), ansi=False)
                + "    "
                + _fmt_stage("BENCH", counts_for_statuses(scen_ben), ansi=False)
            )
            lines.append("")
        lines.append("")

    tg, tt, tb = _compute_totals(rows)
    lines.append("== TOTALS ==")
    lines.append(_fmt_stage("GEN", tg, ansi=False))
    lines.append(_fmt_stage("TEST", tt, ansi=False))
    lines.append(_fmt_stage("BENCH", tb, ansi=False))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_tree(rows: list[SampleRow], *, ansi: bool) -> None:
    tree: dict[str, dict[str, dict[str, dict[str, list[SampleRow]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    for r in rows:
        tree[r.model][r.scenario][r.env][r.variant].append(r)

    for model in sorted(tree.keys()):
        print(_ansi("1", model, enabled=ansi))
        for scenario in sorted(tree[model].keys()):
            print("  " + _ansi("1", scenario, enabled=ansi))
            for env in sorted(tree[model][scenario].keys()):
                print("    " + _ansi("1", env, enabled=ansi))
                for variant in sorted(tree[model][scenario][env].keys()):
                    vrows = tree[model][scenario][env][variant]
                    gen_c = counts_for_statuses(x.gen for x in vrows)
                    tst_c = counts_for_statuses(x.test for x in vrows)
                    ben_c = counts_for_statuses(x.bench for x in vrows)
                    oh = "openhands" if "openhands" in variant.lower() else "single-shot"
                    print("      " + variant + _ansi("2", f" ({oh})", enabled=ansi))
                    print("        " + _fmt_variant_line(gen_c, tst_c, ben_c, ansi=ansi))
        print()

    tg, tt, tb = _compute_totals(rows)
    print(_ansi("1", "== TOTALS ==", enabled=ansi))
    print(_fmt_stage("GEN", tg, ansi=ansi))
    print(_fmt_stage("TEST", tt, ansi=ansi))
    print(_fmt_stage("BENCH", tb, ansi=ansi))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--results-root",
        type=Path,
        default=Path("results"),
        help="Root results directory (default: ./results).",
    )
    ap.add_argument(
        "--out-txt",
        type=Path,
        default=Path("results_summary.txt"),
        help="Plain-text tree output (default: ./results_summary.txt).",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="Optional JSON snapshot path.",
    )
    ap.add_argument(
        "--print",
        dest="do_print",
        action="store_true",
        help="Print colored tree + totals to stdout.",
    )
    ap.add_argument(
        "--no-write-txt",
        action="store_true",
        help="Do not write --out-txt (only useful with --print or --out-json).",
    )
    ap.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors for --print.",
    )
    ap.add_argument(
        "--max-bench-failure-ratio",
        type=float,
        default=0.5,
        help=(
            "Mark a perf run as failed if Locust Aggregated failures/requests exceeds this "
            "(default: 0.5). Requires locust/results/*_stats.csv in the perf dir."
        ),
    )
    args = ap.parse_args()

    root: Path = args.results_root
    if not root.is_dir():
        print(f"results root not found or not a directory: {root}", file=sys.stderr)
        return 2

    rows = collect_samples(root, max_failure_ratio=args.max_bench_failure_ratio)
    if not rows:
        print(f"No samples found under {root}", file=sys.stderr)
        return 1

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ansi = (not args.no_color) and _is_tty()

    try:
        if not args.no_write_txt:
            write_text_tree(
                args.out_txt,
                rows,
                max_failure_ratio=args.max_bench_failure_ratio,
                generated_at=ts,
            )
            if not args.do_print and args.out_json is None:
                print(f"Wrote {args.out_txt}")

        if args.out_json is not None:
            payload = {
                "generated_at": ts,
                "results_root": str(root.resolve()),
                "max_bench_failure_ratio": args.max_bench_failure_ratio,
                "totals": {
                    "GEN": dict(counts_for_statuses(r.gen for r in rows)),
                    "TEST": dict(counts_for_statuses(r.test for r in rows)),
                    "BENCH": dict(counts_for_statuses(r.bench for r in rows)),
                },
                "tree": build_overview_tree(rows),
            }
            args.out_json.parent.mkdir(parents=True, exist_ok=True)
            args.out_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Wrote {args.out_json}")

        if args.do_print:
            print_tree(rows, ansi=ansi)

    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

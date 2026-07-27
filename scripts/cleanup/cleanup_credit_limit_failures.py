#!/usr/bin/env python3
"""
Find (and optionally clean up) k8s_bench experiment iterations that failed because
the LLM provider account ran out of credit/quota, as opposed to a "real" experiment
failure (functional tests failing, docker build errors, deploy timeouts, ...).

The harness does not distinguish a credit/quota outage from any other LLM failure -
it just records a failed iteration and moves on. Two situations then show up on disk:

  1. BASELINE failure: `iteration-000-baseline-failed` and nothing else. The sample
     never produced any usable data, so there's nothing to salvage - the whole
     `k8s-experiments/` dir for that sample should be deleted and re-run.

  2. IN-EXPERIMENT (trailing) failure: some early iterations succeeded, then every
     iteration from some point onward failed (LLM calls erroring out immediately
     once the account was empty). The good iterations are worth keeping - only the
     trailing failed iterations (and their entries in experiment_summary.md /
     llm_cost_ledger.json / conversation.json, if any leaked in) need to go.

Dry-run by default; pass --delete-baseline and/or --fix-tail to act.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Curated, provider-agnostic credit/quota-exhaustion phrases. Extend with --pattern.
CREDIT_PATTERNS = [
    r"credit balance is too low",  # Anthropic
    r"insufficient_quota",  # OpenAI
    r"exceeded your current quota",  # OpenAI
    r"insufficient credits?",  # OpenRouter / generic
    r"quota exceeded",  # generic / GLM / GCP
    r"out of credits?",
    r"payment required",
    r"error code:\s*402\b",
    r"purchase (more )?credits",
    r"top ?up your (account|balance|credits)",
]

# Only these stages ever call an LLM; 04-deploy/05-bench are infra and can contain
# unrelated matches (e.g. locust logs, k8s events) for words like "quota".
LLM_STAGE_FILENAMES = {"phase.log", "codegen.json", "spec_gen.json", "decision.json", "response.log"}
LLM_STAGE_DIR_PREFIXES = ("01-decision", "02-code", "03-spec")


@dataclass
class FailedIteration:
    iteration_dir: Path
    iteration_index: int
    folder: str
    failure_kind: str | None
    failure_reason: str
    is_credit_limit: bool
    matched_snippet: str


@dataclass
class SampleGroup:
    results_dir: Path  # .../sampleN/k8s-experiments/results
    label: str  # model/scenario/env/variant/sampleN
    all_iterations: list[tuple[int, str, bool]]  # (index, folder_name, is_failed)
    credit_failures: list[FailedIteration] = field(default_factory=list)


def _sample_label(results_dir: Path, results_root: Path) -> str:
    try:
        rel = results_dir.relative_to(results_root)
    except ValueError:
        rel = results_dir
    parts = rel.parts
    if "k8s-experiments" in parts:
        idx = parts.index("k8s-experiments")
        parts = parts[:idx]
    return "/".join(parts)


def _gather_text(iteration_dir: Path) -> str:
    chunks = []
    meta = iteration_dir / "meta.json"
    if meta.exists():
        try:
            chunks.append(json.loads(meta.read_text(encoding="utf-8")).get("failure_reason", ""))
        except (json.JSONDecodeError, OSError):
            pass
    for stage_dir in iteration_dir.iterdir():
        if not stage_dir.is_dir() or not stage_dir.name.startswith(LLM_STAGE_DIR_PREFIXES):
            continue
        for f in stage_dir.iterdir():
            if f.name in LLM_STAGE_FILENAMES:
                try:
                    chunks.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
    return "\n".join(chunks)


def _classify(text: str, patterns: list[re.Pattern[str]]) -> str | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            return snippet.lstrip("',: ")
    return None


def _iteration_index(meta_path: Path) -> tuple[int, str, str | None, str]:
    d = json.loads(meta_path.read_text(encoding="utf-8"))
    return (
        int(d.get("iteration_index", -1)),
        str(d.get("folder", meta_path.parent.name)),
        d.get("failure_kind"),
        str(d.get("failure_reason", "")),
    )


def scan(results_root: Path, patterns: list[re.Pattern[str]]) -> list[SampleGroup]:
    groups: dict[Path, SampleGroup] = {}

    iterations_dirs = sorted({p.parent.parent for p in results_root.glob("**/iterations/*/meta.json")})
    for iterations_dir in iterations_dirs:
        results_dir = iterations_dir.parent
        label = _sample_label(results_dir, results_root)
        all_iterations: list[tuple[int, str, bool]] = []
        for it_dir in sorted(iterations_dir.iterdir()):
            meta = it_dir / "meta.json"
            if not meta.exists():
                continue
            try:
                idx, folder, _kind, _reason = _iteration_index(meta)
            except (json.JSONDecodeError, OSError):
                continue
            is_failed = it_dir.name.endswith("-failed")
            all_iterations.append((idx, it_dir.name, is_failed))

        group = SampleGroup(results_dir=results_dir, label=label, all_iterations=sorted(all_iterations))
        groups[results_dir] = group

    for group in groups.values():
        for idx, name, is_failed in group.all_iterations:
            if not is_failed:
                continue
            it_dir = group.results_dir / "iterations" / name
            meta = it_dir / "meta.json"
            try:
                _idx, folder, kind, reason = _iteration_index(meta)
            except (json.JSONDecodeError, OSError):
                continue
            text = _gather_text(it_dir)
            snippet = _classify(text, patterns)
            if snippet is None:
                continue
            group.credit_failures.append(
                FailedIteration(
                    iteration_dir=it_dir,
                    iteration_index=idx,
                    folder=folder,
                    failure_kind=kind,
                    failure_reason=reason,
                    is_credit_limit=True,
                    matched_snippet=snippet,
                )
            )

    return [g for g in groups.values() if g.credit_failures]


def _rm_tree(p: Path) -> None:
    for child in sorted(p.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    for child in sorted([d for d in p.rglob("*") if d.is_dir()], reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass
    p.rmdir()


def _strip_ledger(ledger_path: Path, bad_iteration_ids: set[str]) -> bool:
    if not ledger_path.exists():
        return False
    d = json.loads(ledger_path.read_text(encoding="utf-8"))
    calls = d.get("calls", [])
    kept = [c for c in calls if c.get("iteration_id") not in bad_iteration_ids]
    if len(kept) == len(calls):
        return False

    by_call_type: dict[str, float] = {}
    for c in kept:
        ct = c.get("call_type", "unknown")
        by_call_type[ct] = round(by_call_type.get(ct, 0.0) + float(c.get("estimated_cost_usd", 0.0)), 6)

    d["calls"] = kept
    d["by_call_type"] = by_call_type
    d["by_iteration"] = {k: v for k, v in d.get("by_iteration", {}).items() if k not in bad_iteration_ids}
    d["total_cost_usd"] = round(sum(c.get("estimated_cost_usd", 0.0) for c in kept), 6)
    d["total_input_tokens"] = sum(c.get("input_tokens", 0) for c in kept)
    d["total_output_tokens"] = sum(c.get("output_tokens", 0) for c in kept)
    d["total_cache_read_tokens"] = sum(c.get("cache_read_tokens", 0) for c in kept)
    d["total_cache_write_tokens"] = sum(c.get("cache_write_tokens", 0) for c in kept)

    ledger_path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    return True


def _check_conversation_clean(conversation_path: Path, results_dir: Path, last_good_folder: str | None) -> str | None:
    """Best-effort sanity check: warn (don't edit) if conversation.json looks like it
    has turns beyond the last good iteration. Returns a warning string, or None if OK."""
    if not conversation_path.exists():
        return None
    try:
        d = json.loads(conversation_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "could not parse conversation.json to verify it"
    history = d.get("history", [])
    if not history or last_good_folder is None:
        return None
    last_content = str(history[-1].get("content", ""))
    # The last good iteration's final LLM-stage response.log should match the tail
    # of conversation.json verbatim (see 03-spec/response.log or 02-code/response.log).
    good_dir = results_dir / "iterations" / last_good_folder
    candidates = sorted(good_dir.glob("0*/response.log"), reverse=True)
    for c in candidates:
        try:
            expected = c.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if expected and expected == last_content.strip():
            return None
    return (
        f"conversation.json's last turn does not match {last_good_folder}'s response.log - "
        "it may contain turns from the failed iterations. Left untouched; review manually."
    )


def _trim_summary(summary_path: Path, first_bad_folder: str) -> bool:
    if not summary_path.exists():
        return False
    text = summary_path.read_text(encoding="utf-8")
    marker = f"## {first_bad_folder}"
    idx = text.find(marker)
    if idx == -1:
        return False
    summary_path.write_text(text[:idx].rstrip() + "\n", encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Find k8s_bench iterations that failed because the LLM provider ran out of "
            "credit/quota (as opposed to a real experiment failure). Dry-run by default."
        )
    )
    ap.add_argument("--results-root", type=Path, default=Path("results"), help="Root to scan (default: ./results).")
    ap.add_argument(
        "--pattern", action="append", default=[], help="Extra regex (case-insensitive) to treat as a credit/quota failure."
    )
    ap.add_argument(
        "--delete-baseline",
        action="store_true",
        help="Delete the whole k8s-experiments/ dir for samples whose baseline itself failed on credit/quota (nothing to salvage).",
    )
    ap.add_argument(
        "--fix-tail",
        action="store_true",
        help="For samples with good early iterations followed by a trailing run of credit/quota failures, "
        "delete the failed iteration dirs and trim experiment_summary.md / llm_cost_ledger.json back to the last good iteration.",
    )
    args = ap.parse_args()

    patterns = [re.compile(p, re.IGNORECASE) for p in (CREDIT_PATTERNS + args.pattern)]

    root: Path = args.results_root
    if not root.is_dir():
        raise SystemExit(f"No such results root: {root}")

    groups = scan(root, patterns)
    if not groups:
        print(f"No credit/quota-limit failures found under: {root}")
        return

    baseline_groups = []
    tail_groups = []
    irregular_groups = []

    for g in groups:
        bad_indices = sorted({f.iteration_index for f in g.credit_failures})
        if bad_indices[0] == 0:
            baseline_groups.append(g)
            continue

        first_bad = bad_indices[0]
        max_index = max(idx for idx, _, _ in g.all_iterations)
        tail = [(idx, name, is_failed) for idx, name, is_failed in g.all_iterations if idx >= first_bad]
        if any(not is_failed for _, _, is_failed in tail) or (tail and tail[-1][0] != max_index):
            irregular_groups.append(g)
            continue
        tail_groups.append((g, first_bad, tail))

    if baseline_groups:
        print(f"=== BASELINE credit/quota failures ({len(baseline_groups)}) - no usable data, recommend delete + re-run ===")
        for g in baseline_groups:
            f = g.credit_failures[0]
            print(f"- {g.label}")
            print(f"    {f.folder}: {f.matched_snippet}")
        print()

    if tail_groups:
        print(f"=== IN-EXPERIMENT (trailing) credit/quota failures ({len(tail_groups)}) - early iterations are fine ===")
        for g, first_bad, tail in tail_groups:
            good = [n for i, n, failed in g.all_iterations if i < first_bad]
            last_good = good[-1] if good else None
            bad_folders = [n for _, n, _ in tail]
            print(f"- {g.label}")
            print(f"    last good iteration: {last_good or '(none)'}")
            print(f"    failed tail ({len(bad_folders)}): {', '.join(bad_folders)}")
            print(f"    reason: {g.credit_failures[0].matched_snippet}")
        print()

    if irregular_groups:
        print(f"=== IRREGULAR ({len(irregular_groups)}) - credit/quota failure not a clean trailing run; review manually ===")
        for g in irregular_groups:
            idxs = sorted({f.iteration_index for f in g.credit_failures})
            print(f"- {g.label}  (credit-limit iterations: {idxs})")
        print()

    if not args.delete_baseline and not args.fix_tail:
        print("Dry-run only. Re-run with --delete-baseline and/or --fix-tail to act.")
        return

    if args.delete_baseline and baseline_groups:
        deleted = 0
        for g in baseline_groups:
            k8s_dir = g.results_dir.parent  # sampleN/k8s-experiments
            try:
                if k8s_dir.exists():
                    _rm_tree(k8s_dir)
                    deleted += 1
                    print(f"[deleted] {k8s_dir}")
            except OSError as e:
                print(f"[WARN] failed to delete {k8s_dir}: {e}")
        print(f"Deleted {deleted} baseline-failure sample dir(s).")

    if args.fix_tail and tail_groups:
        fixed = 0
        for g, first_bad, tail in tail_groups:
            good = [n for i, n, failed in g.all_iterations if i < first_bad]
            last_good_folder = good[-1] if good else None

            warning = _check_conversation_clean(g.results_dir / "conversation.json", g.results_dir, last_good_folder)
            if warning:
                print(f"[WARN] {g.label}: {warning}")

            bad_iteration_ids = {f"iteration-{idx:03d}" for idx, _, _ in tail}
            ledger_changed = _strip_ledger(g.results_dir / "llm_cost_ledger.json", bad_iteration_ids)

            first_bad_folder = tail[0][1]
            summary_changed = _trim_summary(g.results_dir / "experiment_summary.md", first_bad_folder)

            for _, name, _ in tail:
                it_dir = g.results_dir / "iterations" / name
                if it_dir.exists():
                    _rm_tree(it_dir)

            print(
                f"[fixed] {g.label}: removed {len(tail)} iteration dir(s), "
                f"ledger_trimmed={ledger_changed}, summary_trimmed={summary_changed}"
            )
            fixed += 1
        print(f"Fixed {fixed} sample(s) with trailing credit/quota failures.")


if __name__ == "__main__":
    main()

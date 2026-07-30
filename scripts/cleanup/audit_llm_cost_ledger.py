#!/usr/bin/env python3
"""
Audit ``llm_cost_ledger.json`` against the raw per-call log lines that
``llm.usage.record_prompter_usage`` writes to each iteration's ``phase.log``.

Background (see ``src/llm/usage.py`` and ``src/k8s_bench/llm_cost.py``):

  - Every LLM call that completes appends one entry to the workspace's
    ``llm_cost_ledger.json`` ("calls" list) via ``append_usage_record``, which
    does a plain read-modify-write of the JSON file (no atomic write, no lock).
    If that process is ever killed mid-write, or the ledger is hand-edited by
    another script, ``load_ledger`` silently falls back to an *empty* ledger on
    the next append - previously recorded calls vanish from the ledger even
    though every other artifact of them survives on disk.
  - The *same* call also logs a line to that iteration's stage ``phase.log``
    (``01-decision/``, ``02-code/`` or ``03-spec/``), always *after* the ledger
    write succeeds:
        "LLM <call_type>: in=<N> (cache: read=<R> write=<W> uncached=<U>,
         hit=<H>%) out=<M> estimated cost ~$<C> (experiment total ~$<T>)"
    and a companion line logged slightly earlier, once per physical provider
    response (including calls later discarded by a retry), from
    ``Prompter.prompt_model``:
        "Estimated LLM cost: $<C> (<N> in + <M> out tokens, model=<MODEL>)"

  This script treats the phase.log lines as the ground truth "call log" and
  the ledger as the derived, crash-unsafe index, then reports every place they
  disagree: ledger entries with no matching log line (usually a sign the
  ledger silently reset and re-recorded, or was hand-edited), log lines with
  no matching ledger entry (a call that was billed/logged but never made it
  into the ledger), and duplicate/ambiguous matches.

Read-only by default. Pass --interactive-delete to review the unmatched
ledger entries and select some for removal (typed confirmation + automatic
backup required before anything is written).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LEDGER_FILENAME = "llm_cost_ledger.json"
ITERATION_ID_RE = re.compile(r"iteration-(\d+)")
LLM_STAGE_DIRS = ("01-decision", "02-code", "03-spec")

_TS = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
LINE_A_RE = re.compile(
    rf"^INFO {_TS} (?:Estimated|Reported) LLM cost: \$(?P<cost>[\d.]+) "
    r"\((?P<in_tok>\d+) in \+ (?P<out_tok>\d+) out tokens, model=(?P<model>\S+)\)"
)
LINE_B_RE = re.compile(
    rf"^INFO {_TS} LLM (?P<call_type>\S+): in=(?P<in_tok>\d+) "
    r"\(cache: read=(?P<cache_read>\d+) write=(?P<cache_write>\d+) uncached=(?P<uncached>\d+), "
    r"hit=(?P<hit>[\d.]+)%\) out=(?P<out_tok>\d+) (?:estimated|reported) cost ~\$(?P<cost>[\d.]+) "
    r"\(experiment total ~\$(?P<total>[\d.]+)\)"
)
LOG_TS_FMT = "%Y-%m-%d %H:%M:%S,%f"

DEFAULT_TOLERANCE_S = 5.0
# Ledger costs are round(raw, 6); phase.log costs are the raw float formatted
# with %.4f. Both come from the same underlying float, but a value that sits
# exactly on a 4th-decimal boundary (e.g. 0.42305) can format down on one side
# and round up on the other due to binary float representation (observed:
# ledger 0.423050 vs. log "$0.4230" for one and the same call). 1 cent-of-a-cent
# absorbs that boundary case without being loose enough to conflate two
# different calls that happen to share input/output token counts.
COST_TOLERANCE_USD = 0.0001


def _round4(x: float) -> float:
    return round(x + 1e-9, 4)


@dataclass
class LedgerCall:
    ledger_id: int  # index into ledger["calls"]
    raw: dict


@dataclass
class LogCall:
    iteration_id: str
    call_type: str
    stage_dir: str
    source: str  # "<phase.log path>:<line no>"
    timestamp_local: datetime | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    uncached_input_tokens: int
    estimated_cost_usd: float
    model: str | None  # recovered from the preceding Line A, if any


@dataclass
class MatchedPair:
    ledger: LedgerCall
    log: LogCall
    ambiguous: bool
    model_mismatch: bool
    timestamp_residual_s: float | None


@dataclass
class AuditResult:
    matched: list[MatchedPair] = field(default_factory=list)
    unmatched_ledger: list[tuple[LedgerCall, str]] = field(default_factory=list)  # (call, reason)
    orphan_logs: list[LogCall] = field(default_factory=list)
    duplicate_ledger_groups: list[list[LedgerCall]] = field(default_factory=list)
    inferred_offset_s: float | None = None
    tolerance_s: float = DEFAULT_TOLERANCE_S


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_ledger(ledger_path: Path) -> tuple[dict, list[LedgerCall]]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    calls = [LedgerCall(ledger_id=i, raw=c) for i, c in enumerate(ledger.get("calls", []))]
    return ledger, calls


def _iteration_id_from_meta_or_name(iteration_dir: Path) -> str:
    meta = iteration_dir / "meta.json"
    if meta.exists():
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
            iid = d.get("iteration_id")
            if iid:
                return str(iid)
        except (json.JSONDecodeError, OSError):
            pass
    m = ITERATION_ID_RE.search(iteration_dir.name)
    return f"iteration-{int(m.group(1)):03d}" if m else iteration_dir.name


def _parse_phase_log(path: Path) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
    """Return (line_a_records, line_b_records), each a list of (line_no, groupdict)."""
    a_records: list[tuple[int, dict]] = []
    b_records: list[tuple[int, dict]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return a_records, b_records
    for i, line in enumerate(lines, start=1):
        m = LINE_B_RE.match(line)
        if m:
            b_records.append((i, m.groupdict()))
            continue
        m = LINE_A_RE.match(line)
        if m:
            a_records.append((i, m.groupdict()))
    return a_records, b_records


def _parse_log_ts(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, LOG_TS_FMT)
    except ValueError:
        return None


def scan_iteration_logs(workspace: Path) -> list[LogCall]:
    """Walk iterations/*/{01-decision,02-code,03-spec}/phase.log and extract call records."""
    calls: list[LogCall] = []
    iterations_dir = workspace / "iterations"
    if not iterations_dir.is_dir():
        return calls

    for iteration_dir in sorted(iterations_dir.iterdir()):
        if not iteration_dir.is_dir():
            continue
        iteration_id = _iteration_id_from_meta_or_name(iteration_dir)
        for stage in LLM_STAGE_DIRS:
            phase_log = iteration_dir / stage / "phase.log"
            if not phase_log.is_file():
                continue
            a_records, b_records = _parse_phase_log(phase_log)
            for line_no, b in b_records:
                cost = float(b["cost"])
                in_tok = int(b["in_tok"])
                out_tok = int(b["out_tok"])
                # Recover model from the nearest preceding Line A with identical
                # (in, out, cost) - both lines are logged from the same
                # `last_usage` object for a given physical call.
                model = None
                for a_line_no, a in reversed(a_records):
                    if a_line_no >= line_no:
                        continue
                    if (
                        int(a["in_tok"]) == in_tok
                        and int(a["out_tok"]) == out_tok
                        and _round4(float(a["cost"])) == _round4(cost)
                    ):
                        model = a["model"]
                        break
                calls.append(
                    LogCall(
                        iteration_id=iteration_id,
                        call_type=b["call_type"],
                        stage_dir=stage,
                        source=f"{phase_log}:{line_no}",
                        timestamp_local=_parse_log_ts(b["ts"]),
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_tokens=int(b["cache_read"]),
                        cache_write_tokens=int(b["cache_write"]),
                        uncached_input_tokens=int(b["uncached"]),
                        estimated_cost_usd=cost,
                        model=model,
                    )
                )
    return calls


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _token_key(in_tok: int, out_tok: int) -> tuple[int, int]:
    return (in_tok, out_tok)


def _ledger_ts(raw: dict) -> datetime | None:
    ts = raw.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def match_calls(
    ledger_calls: list[LedgerCall], log_calls: list[LogCall], *, tolerance_s: float
) -> AuditResult:
    result = AuditResult(tolerance_s=tolerance_s)

    # Group both sides by (iteration_id, call_type); ledger calls missing
    # iteration_id are their own "no iteration_id" bucket (can't be scoped to
    # a log at all).
    def group_key_ledger(c: LedgerCall) -> tuple[str, str]:
        return (str(c.raw.get("iteration_id") or "<none>"), str(c.raw.get("call_type") or "<none>"))

    def group_key_log(c: LogCall) -> tuple[str, str]:
        return (c.iteration_id, c.call_type)

    ledger_groups: dict[tuple[str, str], list[LedgerCall]] = {}
    for c in ledger_calls:
        ledger_groups.setdefault(group_key_ledger(c), []).append(c)

    log_groups: dict[tuple[str, str], list[LogCall]] = {}
    for c in log_calls:
        log_groups.setdefault(group_key_log(c), []).append(c)

    all_keys = set(ledger_groups) | set(log_groups)
    provisional_deltas: list[float] = []
    provisional_matches: list[tuple[LedgerCall, LogCall]] = []

    for key in all_keys:
        iteration_id, call_type = key
        lcalls = list(ledger_groups.get(key, []))
        gcalls = list(log_groups.get(key, []))

        # Bucket unconsumed log calls by exact (input_tokens, output_tokens) -
        # token counts are integers straight from the provider response, so an
        # exact match here is safe. Cost is compared with tolerance afterward
        # to absorb float-formatting boundary cases (see COST_TOLERANCE_USD).
        by_tokens: dict[tuple[int, int], list[LogCall]] = {}
        for g in gcalls:
            by_tokens.setdefault(_token_key(g.input_tokens, g.output_tokens), []).append(g)

        # Detect ledger-side duplicate signatures up front (informational);
        # exact equality is correct here since both entries were written by
        # the same round(x, 6) code path.
        ledger_sig_counts: dict[tuple[float, int, int], list[LedgerCall]] = {}
        for lc in lcalls:
            sig = (
                round(float(lc.raw.get("estimated_cost_usd", 0.0)), 6),
                int(lc.raw.get("input_tokens", 0)),
                int(lc.raw.get("output_tokens", 0)),
            )
            ledger_sig_counts.setdefault(sig, []).append(lc)
        for sig, group in ledger_sig_counts.items():
            if len(group) > 1:
                result.duplicate_ledger_groups.append(group)

        for lc in lcalls:
            if iteration_id == "<none>":
                result.unmatched_ledger.append((lc, "ledger entry has no iteration_id; cannot be scoped to any iteration log"))
                continue

            l_cost = float(lc.raw.get("estimated_cost_usd", 0.0))
            l_in = int(lc.raw.get("input_tokens", 0))
            l_out = int(lc.raw.get("output_tokens", 0))
            tk = _token_key(l_in, l_out)
            token_candidates = by_tokens.get(tk, [])
            candidates = [g for g in token_candidates if abs(g.estimated_cost_usd - l_cost) <= COST_TOLERANCE_USD]

            if not candidates:
                sig = (round(l_cost, 6), l_in, l_out)
                is_dup = len(ledger_sig_counts.get(sig, [])) > 1
                if token_candidates:
                    nearest = min(token_candidates, key=lambda g: abs(g.estimated_cost_usd - l_cost))
                    reason = (
                        f"phase.log has a call for {iteration_id}/{call_type} with matching tokens (in={l_in}, out={l_out}) "
                        f"but cost ${nearest.estimated_cost_usd:.4f} differs from ledger cost ${l_cost:.6f} by more than "
                        f"${COST_TOLERANCE_USD:.4f} (source: {nearest.source})"
                    )
                else:
                    reason = (
                        f"no phase.log line for {iteration_id}/{call_type} has tokens (in={l_in}, out={l_out}); "
                        f"ledger cost=${l_cost:.6f}"
                    )
                if is_dup:
                    reason += "; this ledger cost/token signature is duplicated within the ledger itself (possible double-write)"
                result.unmatched_ledger.append((lc, reason))
                continue

            ambiguous = len(candidates) > 1
            # FIFO among tolerance-matched candidates: consume the earliest-appearing one.
            g = min(candidates, key=lambda c: token_candidates.index(c))
            token_candidates.remove(g)

            model_mismatch = bool(g.model and lc.raw.get("model") and g.model != lc.raw.get("model"))

            residual = None
            l_ts = _ledger_ts(lc.raw)
            if l_ts is not None and g.timestamp_local is not None:
                l_utc_naive = l_ts.astimezone(timezone.utc).replace(tzinfo=None)
                delta = (l_utc_naive - g.timestamp_local).total_seconds()
                provisional_deltas.append(delta)
                provisional_matches.append((lc, g))

            result.matched.append(
                MatchedPair(ledger=lc, log=g, ambiguous=ambiguous, model_mismatch=model_mismatch, timestamp_residual_s=None)
            )

        # Anything left unconsumed in by_tokens is an orphaned log call.
        for remaining in by_tokens.values():
            result.orphan_logs.extend(remaining)

    # Infer a constant local-time -> UTC offset from confident matches, then
    # backfill timestamp_residual_s on every matched pair.
    if provisional_deltas:
        offset = statistics.median(provisional_deltas)
        result.inferred_offset_s = offset
        residual_by_id: dict[int, float] = {}
        for lc, g in provisional_matches:
            l_ts = _ledger_ts(lc.raw)
            if l_ts is None or g.timestamp_local is None:
                continue
            l_utc_naive = l_ts.astimezone(timezone.utc).replace(tzinfo=None)
            delta = (l_utc_naive - g.timestamp_local).total_seconds()
            residual_by_id[lc.ledger_id] = delta - offset
        for mp in result.matched:
            if mp.ledger.ledger_id in residual_by_id:
                mp.timestamp_residual_s = residual_by_id[mp.ledger.ledger_id]

    result.unmatched_ledger.sort(key=lambda t: t[0].ledger_id)
    result.orphan_logs.sort(key=lambda g: (g.iteration_id, g.call_type, g.source))
    return result


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt_ledger_entry(lc: LedgerCall) -> str:
    r = lc.raw
    return (
        f"ledger_id={lc.ledger_id}  ts={r.get('timestamp', '?')}  "
        f"iteration={r.get('iteration_id', '?')}  call_type={r.get('call_type', '?')}  "
        f"model={r.get('model', '?')}  provider={r.get('provider', '?')}  "
        f"tokens(in/out/cache_read/cache_write)="
        f"{r.get('input_tokens', '?')}/{r.get('output_tokens', '?')}/"
        f"{r.get('cache_read_tokens', 0)}/{r.get('cache_write_tokens', 0)}  "
        f"cost=${float(r.get('estimated_cost_usd', 0.0)):.6f}  "
        f"note={r.get('note', '-')}"
    )


def print_report(result: AuditResult, ledger: dict, experiment_label: str) -> None:
    print(f"=== LLM cost ledger audit: {experiment_label} ===")
    print("Matching rules:")
    print("  1. group ledger calls and phase.log call-log lines by (iteration_id, call_type)")
    print(f"  2. within a group, match by exact (input_tokens, output_tokens) and cost within "
          f"+/-${COST_TOLERANCE_USD:.4f} - the ledger and the log line are derived from the same call, "
          f"but a raw cost near a 4th-decimal boundary can format differently between round(x,6) and %.4f")
    print("  3. matches are consumed FIFO in chronological order; leftover ledger entries are 'unmatched', "
          "leftover log lines are 'orphaned'")
    print(f"  4. timestamps are cross-checked only as a secondary signal: a constant local-time -> UTC offset is "
          f"inferred (median over confident matches), then residuals beyond +/-{result.tolerance_s:.1f}s are flagged")
    if result.inferred_offset_s is not None:
        print(f"  inferred phase.log local-time offset vs. ledger UTC timestamps: {result.inferred_offset_s:+.1f}s")
    print(f"  ledger total_cost_usd={ledger.get('total_cost_usd', 0.0):.6f}  "
          f"calls={len(ledger.get('calls', []))}  matched={len(result.matched)}")
    print()

    if not result.unmatched_ledger and not result.orphan_logs and not result.duplicate_ledger_groups:
        n_model_mismatch = sum(1 for m in result.matched if m.model_mismatch)
        n_ts_out = sum(1 for m in result.matched if m.timestamp_residual_s is not None and abs(m.timestamp_residual_s) > result.tolerance_s)
        if not n_model_mismatch and not n_ts_out:
            print("No discrepancies found - every ledger entry matches exactly one logged LLM call.")
            return

    if result.unmatched_ledger:
        print(f"--- Unmatched ledger entries ({len(result.unmatched_ledger)}) - no corresponding logged call found ---")
        for lc, reason in result.unmatched_ledger:
            print(f"  {_fmt_ledger_entry(lc)}")
            print(f"    reason: {reason}")
        print()

    if result.orphan_logs:
        print(f"--- Logged calls with no ledger entry ({len(result.orphan_logs)}) ---")
        for g in result.orphan_logs:
            print(
                f"  source={g.source}  ts_local={g.timestamp_local}  iteration={g.iteration_id}  "
                f"call_type={g.call_type}  model={g.model or '?'}  "
                f"tokens(in/out)={g.input_tokens}/{g.output_tokens}  cost=${g.estimated_cost_usd:.4f}"
            )
        print()

    if result.duplicate_ledger_groups:
        print(f"--- Duplicate ledger entries (identical cost/token signature within the ledger) "
              f"({len(result.duplicate_ledger_groups)} group(s)) ---")
        for group in result.duplicate_ledger_groups:
            print(f"  {len(group)} entries share the same signature:")
            for lc in group:
                print(f"    {_fmt_ledger_entry(lc)}")
        print()

    ambiguous = [m for m in result.matched if m.ambiguous]
    if ambiguous:
        print(f"--- Ambiguous matches (multiple log lines shared the matched entry's cost/token signature) ({len(ambiguous)}) ---")
        for m in ambiguous:
            print(f"  {_fmt_ledger_entry(m.ledger)}")
            print(f"    matched (arbitrarily, earliest-first) to: {m.log.source}")
        print()

    model_mismatches = [m for m in result.matched if m.model_mismatch]
    if model_mismatches:
        print(f"--- Matched calls with a model mismatch ({len(model_mismatches)}) ---")
        for m in model_mismatches:
            print(f"  {_fmt_ledger_entry(m.ledger)}")
            print(f"    log line model={m.log.model!r} vs ledger model={m.ledger.raw.get('model')!r}  (source: {m.log.source})")
        print()

    ts_outliers = [m for m in result.matched if m.timestamp_residual_s is not None and abs(m.timestamp_residual_s) > result.tolerance_s]
    if ts_outliers:
        print(f"--- Matched calls with timestamp residual beyond tolerance (+/-{result.tolerance_s:.1f}s) ({len(ts_outliers)}) ---")
        for m in ts_outliers:
            print(f"  {_fmt_ledger_entry(m.ledger)}")
            print(f"    residual={m.timestamp_residual_s:+.1f}s after applying inferred offset  (source: {m.log.source})")
        print()


# --------------------------------------------------------------------------
# Deletion (interactive only)
# --------------------------------------------------------------------------


def _rebuild_totals(calls: list[dict]) -> dict:
    total_cost = round(sum(float(c.get("estimated_cost_usd", 0.0)) for c in calls), 6)
    by_call_type: dict[str, float] = {}
    by_iteration: dict[str, float] = {}
    for c in calls:
        ct = c.get("call_type", "unknown")
        by_call_type[ct] = round(by_call_type.get(ct, 0.0) + float(c.get("estimated_cost_usd", 0.0)), 6)
        iid = c.get("iteration_id")
        if iid:
            by_iteration[iid] = round(by_iteration.get(iid, 0.0) + float(c.get("estimated_cost_usd", 0.0)), 6)
    return {
        "calls": calls,
        "by_call_type": by_call_type,
        "by_iteration": by_iteration,
        "total_cost_usd": total_cost,
        "total_input_tokens": sum(int(c.get("input_tokens", 0)) for c in calls),
        "total_output_tokens": sum(int(c.get("output_tokens", 0)) for c in calls),
        "total_cache_read_tokens": sum(int(c.get("cache_read_tokens", 0)) for c in calls),
        "total_cache_write_tokens": sum(int(c.get("cache_write_tokens", 0)) for c in calls),
    }


def interactive_delete(ledger_path: Path, ledger: dict, unmatched: list[tuple[LedgerCall, str]]) -> None:
    if not unmatched:
        print("Nothing to delete - no unmatched ledger entries.")
        return

    print("\nSelect unmatched ledger entries to delete.")
    print("Enter comma-separated ledger_id values, 'all', or leave blank to cancel.")
    for lc, reason in unmatched:
        print(f"  [{lc.ledger_id}] {_fmt_ledger_entry(lc)}")
    selection_raw = input("ledger_id(s) to delete: ").strip()
    if not selection_raw:
        print("Cancelled.")
        return

    valid_ids = {lc.ledger_id for lc, _ in unmatched}
    if selection_raw.lower() == "all":
        selected_ids = set(valid_ids)
    else:
        try:
            selected_ids = {int(tok.strip()) for tok in selection_raw.split(",") if tok.strip()}
        except ValueError:
            print("Could not parse selection; expected comma-separated integers. Cancelled.")
            return
        invalid = selected_ids - valid_ids
        if invalid:
            print(f"Refusing: {sorted(invalid)} are not in the unmatched list above. Cancelled.")
            return

    if not selected_ids:
        print("Nothing selected. Cancelled.")
        return

    calls = ledger.get("calls", [])
    to_delete = [c for i, c in enumerate(calls) if i in selected_ids]
    deleted_cost = round(sum(float(c.get("estimated_cost_usd", 0.0)) for c in to_delete), 6)
    original_total = float(ledger.get("total_cost_usd", 0.0))

    print(f"\nAbout to permanently delete {len(to_delete)} ledger entrie(s) totaling ${deleted_cost:.6f}:")
    for i in sorted(selected_ids):
        print(f"  {_fmt_ledger_entry(LedgerCall(ledger_id=i, raw=calls[i]))}")
    confirm = input("Type DELETE to confirm: ").strip()
    if confirm != "DELETE":
        print("Confirmation text did not match. Cancelled - nothing was changed.")
        return

    backup_path = ledger_path.with_name(
        f"{ledger_path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    shutil.copy2(ledger_path, backup_path)
    print(f"Backup written to: {backup_path}")

    remaining = [c for i, c in enumerate(calls) if i not in selected_ids]
    rebuilt = _rebuild_totals(remaining)
    new_ledger = dict(ledger)
    new_ledger.update(rebuilt)
    ledger_path.write_text(json.dumps(new_ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    corrected_total = rebuilt["total_cost_usd"]
    print("\n=== Deletion summary ===")
    print(f"Original total_cost_usd: ${original_total:.6f}")
    print(f"Deleted {len(to_delete)} entrie(s), total ${deleted_cost:.6f}:")
    for i in sorted(selected_ids):
        c = calls[i]
        print(f"  ledger_id={i}  iteration={c.get('iteration_id')}  call_type={c.get('call_type')}  cost=${float(c.get('estimated_cost_usd', 0.0)):.6f}")
    print(f"Corrected total_cost_usd: ${corrected_total:.6f}")
    print(f"Ledger written to: {ledger_path}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _resolve_workspace(path: Path) -> Path:
    """Accept either the ledger file itself or its containing workspace dir."""
    if path.is_file() and path.name == LEDGER_FILENAME:
        return path.parent
    if path.is_dir() and (path / LEDGER_FILENAME).is_file():
        return path
    raise SystemExit(f"Could not find {LEDGER_FILENAME} at or under: {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "workspace",
        type=Path,
        help="Path to llm_cost_ledger.json, or the k8s-experiments/<experiment_id>/results workspace dir containing it.",
    )
    ap.add_argument(
        "--timestamp-tolerance-seconds",
        type=float,
        default=DEFAULT_TOLERANCE_S,
        help=f"Tolerance for flagging matched-but-timestamp-suspicious calls (default: {DEFAULT_TOLERANCE_S}s).",
    )
    ap.add_argument(
        "--interactive-delete",
        action="store_true",
        help="After printing the report, interactively select unmatched ledger entries to delete "
        "(requires typed confirmation; writes a timestamped backup first).",
    )
    args = ap.parse_args()

    workspace = _resolve_workspace(args.workspace)
    ledger_path = workspace / LEDGER_FILENAME

    ledger, ledger_calls = load_ledger(ledger_path)
    log_calls = scan_iteration_logs(workspace)

    experiment_label = str(workspace)
    conv = workspace / "conversation.json"
    if conv.is_file():
        try:
            exp_id = json.loads(conv.read_text(encoding="utf-8")).get("experiment")
            if exp_id:
                experiment_label = f"{workspace}  (experiment_id={exp_id})"
        except (json.JSONDecodeError, OSError):
            pass

    result = match_calls(ledger_calls, log_calls, tolerance_s=args.timestamp_tolerance_seconds)
    print_report(result, ledger, experiment_label)

    if args.interactive_delete:
        interactive_delete(ledger_path, ledger, result.unmatched_ledger)


if __name__ == "__main__":
    main()

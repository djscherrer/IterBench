"""
Rolling high-level summary for one k8s experiment workspace.

Appends to ``experiment_summary.md`` at the workspace root (``sampleN/`` or
``sampleN/k8s-experiments/<slug>/``) after each spec generation and each Locust run.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .feedback import IterationFeedback
from .workspace import (
    find_iteration_spec_path,
    iteration_spec_path,
    k8s_workspace_root,
    normalize_iteration_id,
    parse_iteration_folder_name,
    resolve_k8s_experiment_id,
)
from .spec.models import K8sWorkloadSpec, ResourceSpec

SUMMARY_FILENAME = "experiment_summary.md"
_MAX_NARRATIVE_CHARS = 3500
_LOG_TS_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\]"
)
_ADAPTIVE_PHASE_RE = re.compile(
    r"adaptive phase end t=(\d+)s: (?P<action>.*?) \| "
    r"reqs=(?P<reqs>\d+) fail=(?P<fail>\d+) \((?P<fail_pct>[^)]+)\) "
    r"p\d+=(?P<p95_logged>\S+)"
)
_USERS_FROM_ACTION_RE = re.compile(r"users=(\d+)")
_SHAPE_UPDATE_RE = re.compile(r"Shape test updating to (\d+) users")
_ADAPTIVE_START_USERS_RE = re.compile(
    r"BAXBENCH_ADAPTIVE(?:_V2)?_START_USERS=(\d+)"
)
_STEP_GOODPUT_RE = re.compile(r"step_goodput=([\d.]+)/s")
_STEP_CV_RE = re.compile(r"\bcv=([\d.]+)")
_ADAPTIVE_V2_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) "
    r"final_users=(?P<final_users>\S+) "
    r"low_ok=(?P<low_ok>\S+) "
    r"high_bad=(?P<high_bad>\S+) "
    r"goodput_history=\[(?P<history>[^\]]*)\]"
)


def experiment_summary_path(sample_dir: Path) -> Path:
    return k8s_workspace_root(sample_dir) / SUMMARY_FILENAME


def _utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ensure_header(path: Path, *, sample_dir: Path, load_profile: str | None = None) -> None:
    if path.is_file() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    experiment = resolve_k8s_experiment_id()
    profile = (load_profile or os.environ.get("BAXBENCH_LOAD_PROFILE", "")).strip() or "default"
    header = "\n".join(
        [
            "# K8s experiment summary",
            "",
            f"- **Experiment**: `{experiment}`",
            f"- **Workspace**: `{k8s_workspace_root(sample_dir)}`",
            f"- **Started**: {_utc_now_label()}",
            f"- **Load profile**: `{profile}`",
            "",
            "Each iteration below has a **spec generation** block (LLM deployment + rationale) "
            "and a **Locust run** block (adaptive ramp table when applicable).",
            "",
            f"- **LLM cost ledger**: `{k8s_workspace_root(sample_dir) / 'llm_cost_ledger.json'}` "
            "(estimated; set `BAXBENCH_LLM_MAX_COST` to cap spend)",
            "",
            "---",
            "",
        ]
    )
    path.write_text(header, encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def _iteration_heading_present(path: Path, iteration_id: str) -> bool:
    if not path.is_file():
        return False
    iid = normalize_iteration_id(iteration_id)
    return re.search(rf"^## {re.escape(iid)}\s*$", path.read_text(encoding="utf-8"), re.M) is not None


def _maybe_write_iteration_heading(path: Path, iteration_id: str) -> str:
    iid = normalize_iteration_id(iteration_id)
    if _iteration_heading_present(path, iid):
        return ""
    return f"\n## {iid}\n\n"


def _extract_llm_narrative(raw_response: str) -> str:
    """Text before the machine-readable SPEC / YAML block."""
    raw = (raw_response or "").strip()
    if not raw:
        return "(no LLM narrative in spec_gen.log)"

    for pattern in (
        re.compile(r"<SPEC>\s*", re.I),
        re.compile(r"```(?:ya?ml)?\s*\n", re.I),
    ):
        m = pattern.search(raw)
        if m and m.start() > 0:
            text = raw[: m.start()].strip()
            if text:
                raw = text
                break

    if len(raw) > _MAX_NARRATIVE_CHARS:
        return raw[:_MAX_NARRATIVE_CHARS].rstrip() + "\n\n…(truncated)"
    return raw


def _format_resources(label: str, res: ResourceSpec) -> str:
    return (
        f"{label}: cpu {res.cpu_request}/{res.cpu_limit}, "
        f"mem {res.memory_request}/{res.memory_limit}"
    )


def _spec_bullets(spec: K8sWorkloadSpec) -> list[str]:
    b = spec.backend
    lines = [
        f"- **Namespace**: `{spec.namespace}`",
        f"- **Backend replicas**: {b.replicas}",
        f"- **Backend** {_format_resources('resources', b.resources)}",
    ]
    if spec.database.enabled:
        lines.append(
            f"- **Database** {_format_resources('resources', spec.database.resources)}"
        )
        lines.append(f"- **Postgres replicas**: {spec.database.replicas}")
        if spec.database.replicas > 1:
            lines.append(
                f"- **Postgres topology**: 1 primary + "
                f"{spec.database.replicas - 1} read replica(s) (streaming replication)"
            )
        lines.append(f"- **Postgres max_connections**: {spec.database.max_connections}")
        if spec.database.placement_worker:
            lines.append(
                f"- **Postgres placement (pin)**: `{spec.database.placement_worker}`"
            )
        elif spec.database.placement_workers:
            lines.append(
                "- **Postgres placement (allow-list)**: "
                + ", ".join(spec.database.placement_workers)
            )
    else:
        lines.append("- **Database**: disabled")
    if spec.backend.placement_workers:
        lines.append(
            f"- **Backend placement workers**: {', '.join(spec.backend.placement_workers)}"
        )
    lines.append(f"- **Backend spread_replicas**: {spec.backend.spread_replicas}")
    return lines


def _previous_spec_path(iteration_path: Path) -> Path | None:
    """Locate the previous iteration's ``spec.yaml`` (suffix-aware, allows baseline 000)."""
    phase, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
    if phase is None or phase <= 0:
        return None

    parent = iteration_path.parent
    if not parent.is_dir():
        return None

    prev_phase = phase - 1
    candidates: list[Path] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        p, _k, failed = parse_iteration_folder_name(child.name)
        if p != prev_phase or failed:
            continue
        candidates.append(child)

    candidates.sort(key=lambda c: c.name)
    for cand in candidates:
        spec = find_iteration_spec_path(cand)
        if spec is not None:
            return spec
    return None


def _diff_field(name: str, old: str | int, new: str | int) -> str | None:
    if old == new:
        return None
    return f"- **{name}**: `{old}` → `{new}`"


def _spec_diff_markdown(prev: K8sWorkloadSpec, cur: K8sWorkloadSpec) -> str:
    changes: list[str] = []
    for line in (
        _diff_field("backend replicas", prev.backend.replicas, cur.backend.replicas),
        _diff_field(
            "backend cpu limit",
            prev.backend.resources.cpu_limit,
            cur.backend.resources.cpu_limit,
        ),
        _diff_field(
            "backend cpu request",
            prev.backend.resources.cpu_request,
            cur.backend.resources.cpu_request,
        ),
        _diff_field(
            "backend memory limit",
            prev.backend.resources.memory_limit,
            cur.backend.resources.memory_limit,
        ),
        _diff_field("database enabled", prev.database.enabled, cur.database.enabled),
        _diff_field(
            "database cpu limit",
            prev.database.resources.cpu_limit,
            cur.database.resources.cpu_limit,
        ),
        _diff_field(
            "database memory limit",
            prev.database.resources.memory_limit,
            cur.database.resources.memory_limit,
        ),
    ):
        if line:
            changes.append(line)
    if not changes:
        return "No resource/replica changes vs previous iteration."
    return "\n".join(changes)


def _gather_perf_log_text(perf_run_dir: Path) -> str:
    """Merge bench.log and fetched Locust loader logs (adaptive lines live on the master)."""
    chunks: list[str] = []
    bench = perf_run_dir / "bench.log"
    if bench.is_file():
        chunks.append(bench.read_text(encoding="utf-8", errors="replace"))
    logs_root = perf_run_dir / "logs"
    if logs_root.is_dir():
        for path in sorted(logs_root.rglob("locust-*.log")):
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _parse_bench_log_times(bench_log: str) -> tuple[str | None, str | None]:
    first: str | None = None
    last: str | None = None
    for line in bench_log.splitlines():
        m = _LOG_TS_RE.match(line)
        if not m:
            continue
        ts = m.group(1).replace(",", ".")
        if first is None:
            first = ts
        last = ts
    return first, last


def _parse_initial_users(bench_log: str) -> int | None:
    """Find the user count at the start of the run (level 0)."""
    for line in bench_log.splitlines():
        m = _SHAPE_UPDATE_RE.search(line)
        if m:
            return int(m.group(1))
        m = _ADAPTIVE_START_USERS_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def _parse_adaptive_phases(bench_log: str) -> list[dict[str, Any]]:
    """Parse adaptive ``phase end`` lines, dedup by ``t_s`` (master log + bench.log overlap)."""
    seen_t: set[int] = set()
    phases: list[dict[str, Any]] = []
    for line in bench_log.splitlines():
        if "adaptive phase end" not in line:
            continue
        m = _ADAPTIVE_PHASE_RE.search(line)
        if not m:
            continue
        t_s = int(m.group(1))
        if t_s in seen_t:
            continue
        seen_t.add(t_s)
        users_m = _USERS_FROM_ACTION_RE.search(m.group("action"))
        p95_decision = re.search(r"p95=(\d+)ms", m.group("action"))
        goodput_m = _STEP_GOODPUT_RE.search(line)
        cv_m = _STEP_CV_RE.search(line)
        phases.append(
            {
                "t_s": t_s,
                "next_users": int(users_m.group(1)) if users_m else None,
                "p95_decision_ms": int(p95_decision.group(1)) if p95_decision else None,
                "p95_logged": m.group("p95_logged"),
                "reqs": int(m.group("reqs")),
                "fail": int(m.group("fail")),
                "fail_pct": m.group("fail_pct"),
                "action": m.group("action").strip(),
                "step_goodput_rps": float(goodput_m.group(1)) if goodput_m else None,
                "step_cv": float(cv_m.group(1)) if cv_m else None,
            }
        )
    phases.sort(key=lambda p: p["t_s"])
    return phases


def _parse_adaptive_v2_stop(bench_log: str) -> dict[str, Any] | None:
    """Pull the final ``adaptive-v2 stop:`` line, if present."""
    last: dict[str, Any] | None = None
    for line in bench_log.splitlines():
        m = _ADAPTIVE_V2_STOP_RE.search(line)
        if not m:
            continue
        last = {
            "reason": m.group("reason"),
            "final_users": m.group("final_users"),
            "low_ok": m.group("low_ok"),
            "high_bad": m.group("high_bad"),
            "history": m.group("history").strip(),
        }
    return last


def _adaptive_table_markdown(
    phases: list[dict[str, Any]],
    *,
    initial_users: int | None = None,
    v2_stop: dict[str, Any] | None = None,
) -> str:
    """
    Render the adaptive ramp as a step table.

    - Step 0 = initial level (before any decision); step N = level reached after
      the N-th decision.
    - ``level users`` = virtual users actually running during this measurement
      window (= previous step's ``next users``).
    - ``→ next users`` = users the controller selected for the next window.
    - ``reqs`` / ``fail %`` are **per-step deltas** (not cumulative since t=0).
    - ``goodput`` and ``cv`` only appear for ``k8s-adaptive-v2`` runs.
    """
    if not phases:
        return "(no `adaptive phase end` lines in bench.log — not an adaptive profile or run too short)"

    has_goodput = any(p.get("step_goodput_rps") is not None for p in phases)
    has_cv = any(p.get("step_cv") is not None for p in phases)

    header_cols = [
        "Step",
        "window end t (s)",
        "level users",
        "→ next users",
        "p95 decision (ms)",
        "p95 logged",
        "reqs",
        "fail %",
    ]
    if has_goodput:
        header_cols.append("goodput (succ/s)")
    if has_cv:
        header_cols.append("cv")
    header_cols.append("action")
    header = "| " + " | ".join(header_cols) + " |"
    sep_cells = ["---:"] * (len(header_cols) - 1) + ["---"]
    sep = "|" + "|".join(sep_cells) + "|"
    rows = [header, sep]

    level_users = initial_users
    cumulative_reqs = 0
    cumulative_fail = 0
    for i, p in enumerate(phases):
        delta_reqs = max(0, p["reqs"] - cumulative_reqs)
        delta_fail = max(0, p["fail"] - cumulative_fail)
        cumulative_reqs = p["reqs"]
        cumulative_fail = p["fail"]
        step_fail_pct = (
            f"{100.0 * delta_fail / delta_reqs:.1f}%"
            if delta_reqs > 0
            else p["fail_pct"]
        )
        next_users = p["next_users"]
        cells = [
            str(i),
            str(p["t_s"]),
            str(level_users if level_users is not None else "—"),
            str(next_users if next_users is not None else "—"),
            str(p["p95_decision_ms"] if p["p95_decision_ms"] is not None else "—"),
            str(p["p95_logged"]),
            str(delta_reqs),
            step_fail_pct,
        ]
        if has_goodput:
            gp = p.get("step_goodput_rps")
            cells.append(f"{gp:.1f}" if gp is not None else "—")
        if has_cv:
            cv = p.get("step_cv")
            cells.append(f"{cv:.2f}" if cv is not None else "—")
        cells.append(p["action"].replace("|", "\\|")[:80])
        rows.append("| " + " | ".join(cells) + " |")
        level_users = next_users

    last = phases[-1]
    rows.append("")
    if cumulative_reqs:
        rows.append(
            f"**Ramp outcome**: last decision selected **{last['next_users']}** users "
            f"@ t={last['t_s']}s ({cumulative_reqs} cumulative reqs, "
            f"{100.0 * cumulative_fail / cumulative_reqs:.1f}% cumulative failures)."
        )
    else:
        rows.append(
            f"**Ramp outcome**: last decision selected **{last['next_users']}** users "
            f"@ t={last['t_s']}s."
        )
    if v2_stop is not None:
        rows.append(
            f"**Adaptive-v2 stop**: reason=`{v2_stop['reason']}` "
            f"final_users=**{v2_stop['final_users']}** "
            f"bracket=[{v2_stop['low_ok']}…{v2_stop['high_bad']}]"
        )
        if v2_stop.get("history"):
            rows.append(f"Recent goodput: {v2_stop['history']}")
    if any("bracket" in p["action"] or "stopping" in p["action"] for p in phases):
        rows.append("Adaptive controller reached a bracket / stop condition.")
    if any("abort" in line for line in (p["action"] for p in phases)):
        rows.append("⚠ Early abort detected in adaptive log.")
    return "\n".join(rows)


def _aggregate_locust_line(perf_run_dir: Path) -> str:
    for stats_path in sorted(perf_run_dir.glob("bench_results_*_stats.csv")):
        with stats_path.open(newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        total_req = total_fail = 0
        rps = 0.0
        for row in rows:
            try:
                total_req += int(float(row.get("Request Count") or 0))
                total_fail += int(float(row.get("Failure Count") or 0))
                rps += float(row.get("Requests/s") or 0)
            except (TypeError, ValueError):
                pass
        fail_pct = f"{100.0 * total_fail / total_req:.1f}%" if total_req else "n/a"
        return (
            f"**Locust aggregate** ({stats_path.name}): {total_req} requests, "
            f"{total_fail} failures ({fail_pct}), ~{rps:.1f} req/s summed across endpoints."
        )
    return "(no bench_results_*_stats.csv found)"


def append_spec_generation_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    spec: K8sWorkloadSpec,
    raw_response: str,
    warnings: list[str],
    had_prior_feedback: bool,
    phase_index: int,
    load_profile: str | None = None,
) -> Path:
    """Append spec-generation subsection for one iteration."""
    if os.environ.get("BAXBENCH_K8S_EXPERIMENT_SUMMARY", "true").lower() in (
        "0",
        "false",
        "no",
    ):
        return experiment_summary_path(sample_dir)

    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)

    prev_path = _previous_spec_path(iteration_path)
    if prev_path:
        try:
            diff_text = _spec_diff_markdown(
                K8sWorkloadSpec.from_yaml_file(prev_path), spec
            )
            diff_source = f"`{prev_path.parent.name}`"
        except ValueError:
            diff_text = f"(could not load previous spec at `{prev_path}`)"
            diff_source = prev_path.parent.name
    else:
        diff_text = "First iteration in this experiment (no prior spec to diff)."
        diff_source = "—"

    narrative = _extract_llm_narrative(raw_response)
    warn_block = ""
    if warnings:
        warn_block = "\n**Validation warnings**\n" + "\n".join(f"- {w}" for w in warnings) + "\n"

    body = "\n".join(
        [
            _maybe_write_iteration_heading(path, iid),
            f"### Spec generation ({_utc_now_label()})",
            "",
            f"- **Phase**: {phase_index}",
            f"- **Prior Locust feedback in prompt**: {'yes' if had_prior_feedback else 'no (first phase)'}",
            f"- **Spec path**: `{iteration_spec_path(iteration_path)}`",
            "",
            "**Deployment**",
            "",
            "\n".join(_spec_bullets(spec)),
            "",
            f"**Changes vs {diff_source}**",
            "",
            diff_text,
            "",
            "**LLM rationale** (from `spec_gen.log`, text before `<SPEC>`)",
            "",
            narrative,
            warn_block,
            "",
            "---",
            "",
        ]
    )
    _append(path, body)
    return path


def append_perf_run_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    perf_run_dir: Path,
    feedback: IterationFeedback | None = None,
    load_profile: str | None = None,
) -> Path:
    """Append Locust / adaptive perf subsection for one iteration."""
    if os.environ.get("BAXBENCH_K8S_EXPERIMENT_SUMMARY", "true").lower() in (
        "0",
        "false",
        "no",
    ):
        return experiment_summary_path(sample_dir)

    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)

    bench_log_path = perf_run_dir / "bench.log"
    log_text = _gather_perf_log_text(perf_run_dir)

    t0, t1 = _parse_bench_log_times(log_text)
    if t0 and t1:
        time_range = f"{t0} – {t1}"
    else:
        time_range = perf_run_dir.name

    phases = _parse_adaptive_phases(log_text)
    initial_users = _parse_initial_users(log_text)
    v2_stop = _parse_adaptive_v2_stop(log_text)
    adaptive_md = _adaptive_table_markdown(
        phases, initial_users=initial_users, v2_stop=v2_stop
    )
    locust_line = _aggregate_locust_line(perf_run_dir)

    locust_table = ""
    fb = feedback
    if fb is None:
        from .feedback import load_feedback_from_run_dir

        fb = load_feedback_from_run_dir(perf_run_dir)

    if fb and fb.locust_summary and "(no Locust stats" not in fb.locust_summary:
        locust_table = f"\n**Per-endpoint Locust** (from feedback)\n\n{fb.locust_summary}\n"

    error_lines = ""
    if fb and fb.error_excerpt and fb.error_excerpt.strip() not in (
        "(no error report)",
        "",
    ):
        excerpt = fb.error_excerpt.strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200] + "\n…(truncated)"
        error_lines = f"\n**Top errors**\n\n```\n{excerpt}\n```\n"

    pod_hint = ""
    if fb and fb.pod_utilization:
        pod_hint = _format_pod_utilization_for_summary(fb.pod_utilization)

    notes_line = ""
    if fb and fb.notes and fb.notes.strip():
        notes_line = f"\n**Notes**\n\n{fb.notes.strip()}\n"

    body = "\n".join(
        [
            _maybe_write_iteration_heading(path, iid),
            f"### Locust run ({time_range})",
            "",
            f"- **Recorded**: {_utc_now_label()}",
            f"- **Perf directory**: `{perf_run_dir}`",
            f"- **bench.log**: `{bench_log_path}`",
            "",
            locust_line,
            locust_table,
            "",
            "**Adaptive ramp** (from `bench.log` + `logs/**/locust-*.log`)",
            "",
            adaptive_md,
            error_lines,
            pod_hint,
            notes_line,
            "",
            "---",
            "",
        ]
    )
    _append(path, body)
    return path


def _format_pod_utilization_for_summary(text: str) -> str:
    """Render the ``pod_utilization`` payload as a collapsible Markdown section."""
    body = (text or "").strip()
    if not body:
        return ""
    if "unavailable" in body.splitlines()[0].lower():
        return f"\n**K8s utilization**: {body.splitlines()[0]}\n"
    lines = [
        "",
        "<details>",
        "<summary><strong>K8s utilization</strong> (kubectl top during the run)</summary>",
        "",
        body,
        "",
        "</details>",
        "",
    ]
    return "\n".join(lines)


def append_refinement_decision_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    decision: Any,
    load_profile: str | None = None,
) -> Path:
    """Append deployment-vs-code decision before a phase's spec generation."""
    if os.environ.get("BAXBENCH_K8S_EXPERIMENT_SUMMARY", "true").lower() in (
        "0",
        "false",
        "no",
    ):
        return experiment_summary_path(sample_dir)

    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)
    body = "\n".join(
        [
            _maybe_write_iteration_heading(path, iid),
            f"### Refinement decision (phase {decision.phase_index})",
            "",
            f"- **Recorded**: {_utc_now_label()}",
            f"- **Action**: `{decision.action}`",
            f"- **Based on**: `{decision.based_on_iteration}`",
            f"- **Rationale**: {decision.rationale.strip()}",
            "",
            "---",
            "",
        ]
    )
    _append(path, body)
    return path

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
from .paths import (
    iteration_id_for_phase,
    iteration_spec_path,
    k8s_configs_root,
    k8s_workspace_root,
    normalize_iteration_id,
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


def experiment_summary_path(sample_dir: Path) -> Path:
    return k8s_workspace_root(sample_dir) / SUMMARY_FILENAME


def _utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ensure_header(path: Path, *, sample_dir: Path, load_profile: str | None = None) -> None:
    if path.is_file() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    experiment = resolve_k8s_experiment_id() or "(legacy — no experiment slug)"
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
    iid = normalize_iteration_id(iteration_path.name)
    m = re.fullmatch(r"iteration-(\d+)", iid)
    if not m or int(m.group(1)) <= 1:
        return None
    prev_id = iteration_id_for_phase(int(m.group(1)) - 1)
    prev = iteration_path.parent / prev_id / "spec.yaml"
    return prev if prev.is_file() else None


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


def _parse_adaptive_phases(bench_log: str) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for line in bench_log.splitlines():
        if "adaptive phase end" not in line:
            continue
        m = _ADAPTIVE_PHASE_RE.search(line)
        if not m:
            continue
        users_m = _USERS_FROM_ACTION_RE.search(m.group("action"))
        p95_decision = re.search(r"p95=(\d+)ms", m.group("action"))
        phases.append(
            {
                "t_s": int(m.group(1)),
                "users": int(users_m.group(1)) if users_m else None,
                "p95_decision_ms": int(p95_decision.group(1)) if p95_decision else None,
                "p95_logged": m.group("p95_logged"),
                "reqs": int(m.group("reqs")),
                "fail": int(m.group("fail")),
                "fail_pct": m.group("fail_pct"),
                "action": m.group("action").strip(),
            }
        )
    return phases


def _adaptive_table_markdown(phases: list[dict[str, Any]]) -> str:
    if not phases:
        return "(no `adaptive phase end` lines in bench.log — not an adaptive profile or run too short)"
    header = "| Step | t (s) | users | p95 decision (ms) | p95 logged | reqs | fail % | action |"
    sep = "|---:|---:|---:|---:|---:|---:|---:|---|"
    rows = [header, sep]
    for i, p in enumerate(phases, start=1):
        rows.append(
            "| "
            + " | ".join(
                [
                    str(i),
                    str(p["t_s"]),
                    str(p["users"] if p["users"] is not None else "—"),
                    str(p["p95_decision_ms"] if p["p95_decision_ms"] is not None else "—"),
                    str(p["p95_logged"]),
                    str(p["reqs"]),
                    str(p["fail_pct"]),
                    p["action"].replace("|", "\\|")[:80],
                ]
            )
            + " |"
        )
    last = phases[-1]
    rows.append("")
    rows.append(
        f"**Ramp outcome**: last step **{last['users']}** users @ t={last['t_s']}s, "
        f"**{last['fail_pct']}** failures ({last['reqs']} reqs)."
    )
    if any("bracket" in p["action"] or "stopping shape" in p["action"] for p in phases):
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
    adaptive_md = _adaptive_table_markdown(phases)
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
        first_line = fb.pod_utilization.splitlines()[0] if fb.pod_utilization else ""
        if first_line and "unavailable" not in first_line.lower():
            pod_hint = f"\n**K8s utilization**: captured ({first_line[:120]}…).\n"

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
            "",
            "---",
            "",
        ]
    )
    _append(path, body)
    return path

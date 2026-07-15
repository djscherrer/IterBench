"""
Collect benchmark feedback for the next K8s spec-generation iteration.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench_diagnostics.summary import (
    read_run_config,
    summarize_load_run,
    summarize_run_dir,
)

from .spec.models import K8sWorkloadSpec
from .workspace import (
    deploy_probe_record_path,
    find_iteration_spec_path,
    iteration_bench_dir,
    iteration_functional_tests_dir,
)


@dataclass(frozen=True)
class IterationFeedback:
    """
    Structured summary of one prior iteration for prompts.

    Success path: Locust + diagnostics for the decision stage.
    Code/spec content is referenced via conversation history pointers, not
    inlined here. Prior-iteration failures are carried via
    :class:`~k8s_bench.failure.IterationFailure` in lineage, not here.
    """

    iteration_id: str
    perf_run_dir: str
    locust_summary: str
    error_excerpt: str
    load_run_summary: str = ""
    diagnostics_summary: str = ""
    notes: str = ""
    status: str = "success"  # "success" | "failed"
    failure_reason: str = ""
    failure_kind: str = ""  # "spec" | "code" | "deploy" | "" for success

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def to_prompt_text(self) -> str:
        if self.is_failed:
            return self._to_prompt_text_failed()
        return self._to_prompt_text_success()

    def load_test_prompt_text(self) -> str:
        """Load-test block shared by iteration feedback and experiment summary."""
        return "\n".join(
            [
                "### Adaptive ramp",
                "Phase-by-phase controller narrative (per-second samples omitted).",
                "",
                self.load_run_summary.strip() or "(load run details unavailable)",
                "",
                "### Locust (per endpoint)",
                "Source: Locust ``locust/results/<test>_stats.csv`` "
                "(includes p95/p99 from Locust percentiles).",
                "",
                self.locust_summary or "(no Locust stats found)",
                "",
                "### Locust HTTP errors",
                "Client-side failure messages from Locust "
                "(often generic 500s; see diagnostics for root cause).",
                "",
                self.error_excerpt or "(no Locust error report)",
            ]
        )

    def load_test_summary_text(self) -> str:
        """Load-test details for experiment summary (adaptive ramp shown as plot)."""
        return "\n".join(
            [
                "### Locust (per endpoint)",
                "Source: Locust ``locust/results/<test>_stats.csv`` "
                "(includes p95/p99 from Locust percentiles).",
                "",
                self.locust_summary or "(no Locust stats found)",
                "",
                "### Locust HTTP errors",
                "Client-side failure messages from Locust "
                "(often generic 500s; see diagnostics for root cause).",
                "",
                self.error_excerpt or "(no Locust error report)",
            ]
        )

    def diagnostics_prompt_text(self) -> str:
        """Diagnostics block shared by iteration feedback and experiment summary."""
        return "\n".join(
            [
                "Pod logs, then run-scoped metrics (PostgreSQL, replication, pooler, "
                "cache, pod health, cluster events, ``kubectl top``). "
                "Bursty metrics use **min / p50 / avg / p95 / max** over samples.",
                "",
                self.diagnostics_summary.strip() or "(no diagnostics collected)",
            ]
        )

    def _to_prompt_text_success(self) -> str:
        parts = [
            "## Load test results",
            "",
            self.load_test_prompt_text(),
            "",
            "## Diagnostics",
            self.diagnostics_prompt_text(),
        ]
        if self.notes:
            parts.extend(["", "## Notes", self.notes])
        return "\n".join(parts)

    def _to_prompt_text_failed(self) -> str:
        """Render feedback when the prior iteration failed before producing benchmark data."""
        kind_label = self.failure_kind or "iteration"
        parts = [
            f"Previous iteration: `{self.iteration_id}` — **FAILED** before benchmark.",
            "",
            "**No Locust or Kubernetes utilization data is available** for this "
            "iteration. Read the failure reason and error excerpt, then propose a "
            "fix that addresses the *cause*.",
            "",
            "## Failure",
            f"- **Stage**: `{kind_label}` "
            "(`baseline`/`spec`/`deploy` = manifest/cluster could not be deployed; "
            "`code` = functional tests did not pass after code refinement)",
            f"- **Reason**: {self.failure_reason or '(unspecified)'}",
        ]
        parts.extend(
            [
                "",
                "## Error excerpt",
                self.error_excerpt.strip() or "(no error excerpt captured)",
                "",
                "## Locust results",
                "(no Locust data — benchmark did not run for this iteration)",
                "",
                "## Kubernetes utilization",
                "(no kubectl-top data — benchmark did not run for this iteration)",
            ]
        )
        if self.notes:
            parts.extend(["", "## Notes", self.notes])
        return "\n".join(parts)


def _format_locust_stats_csv(stats_path: Path) -> str:
    """Format ``locust/results/<test>_stats.csv`` as a markdown table (LLM-friendly vs raw CSV)."""
    with stats_path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""

    header = (
        "| Endpoint | Requests | Failures | Fail % | Med ms | Avg ms | Min ms | Max ms | "
        "P95 ms | P99 ms | Req/s | Fail/s |"
    )
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    body: list[str] = [header, sep]

    for row in rows:
        name = (row.get("Name") or "").strip() or "Aggregated"
        try:
            req = int(float(row.get("Request Count") or 0))
            fail = int(float(row.get("Failure Count") or 0))
        except (TypeError, ValueError):
            req, fail = 0, 0
        fail_pct = f"{100.0 * fail / req:.1f}%" if req else "0%"

        def _ms(key: str) -> str:
            raw = row.get(key) or ""
            try:
                v = float(raw)
                return str(int(round(v)))
            except (TypeError, ValueError):
                return str(raw).strip() or "-"

        def _rate(key: str) -> str:
            raw = row.get(key) or ""
            try:
                return f"{float(raw):.1f}"
            except (TypeError, ValueError):
                return str(raw).strip() or "-"

        body.append(
            "| "
            + " | ".join(
                [
                    name.replace("|", "\\|"),
                    str(req),
                    str(fail),
                    fail_pct,
                    _ms("Median Response Time"),
                    _ms("Average Response Time"),
                    _ms("Min Response Time"),
                    _ms("Max Response Time"),
                    _ms("95%"),
                    _ms("99%"),
                    _rate("Requests/s"),
                    _rate("Failures/s"),
                ]
            )
            + " |"
        )

    return "\n".join(body)


def _extract_locust_table_from_bench_log(bench_log: str) -> str:
    """Locust shutdown stats table (fallback if CSV missing)."""
    lines = bench_log.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if "Type" in line and "Name" in line and "# reqs" in line:
            start = i
            break
    if start is None:
        return ""

    out: list[str] = [lines[start].rstrip()]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("Response time percentiles"):
            break
        if stripped.startswith("Error report"):
            break
        out.append(line.rstrip())
    return "\n".join(out)


def _locust_summary_from_run_dir(perf_run_dir: Path, bench_log: str) -> str:
    candidates = sorted((perf_run_dir / "locust" / "results").glob("*_stats.csv"))
    if candidates:
        table = _format_locust_stats_csv(candidates[0])
        if table:
            return table
    table = _extract_locust_table_from_bench_log(bench_log)
    if table:
        return f"(fallback: bench.log)\n\n```\n{table}\n```"
    return ""


def _extract_error_excerpt(bench_log: str, *, max_lines: int = 15) -> str:
    """Locust error report only — stop before bench INFO/scp lines."""
    marker = "Error report"
    idx = bench_log.find(marker)
    if idx < 0:
        return ""
    out: list[str] = []
    for line in bench_log[idx:].splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            if out:
                break
            continue
        if re.match(r"^(INFO|WARNING|DEBUG|ERROR)\s+\d{4}-", stripped):
            break
        if stripped.startswith("Type ") and "Name" in stripped:
            break
        if stripped.startswith("Response time percentiles"):
            break
        out.append(line.rstrip())
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def _replica_usage_note(
    utilization_text: str,
    spec: K8sWorkloadSpec | None,
) -> str:
    """
    Flag the common pitfall: spec deployed read replicas but the app code only
    talks to the primary (replicas idle while primary saturates).
    """
    if spec is None or not spec.database.enabled or spec.database.replicas <= 1:
        return ""
    if not utilization_text or utilization_text == "(kubernetes metrics unavailable)":
        return ""

    primary_max_m: int | None = None
    replica_max_m: int | None = None
    for line in utilization_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "/" not in stripped:
            continue
        cols = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cols) < 2:
            continue
        pod = cols[0]
        cpu_field = cols[1]
        try:
            max_cpu = int(cpu_field.split("/")[-1])
        except (ValueError, IndexError):
            continue
        is_replica = "-replica-" in pod or pod.endswith("-replica")
        is_primary = (
            pod.startswith(spec.database.service_name)
            and not is_replica
            and "postgres" in pod
        ) or (pod.startswith("postgres-") and not is_replica)
        if is_replica:
            replica_max_m = max(replica_max_m or 0, max_cpu)
        elif is_primary:
            primary_max_m = max(primary_max_m or 0, max_cpu)

    if primary_max_m is None or replica_max_m is None:
        return ""

    if primary_max_m >= 200 and replica_max_m < max(50, primary_max_m // 10):
        return (
            f"Read replicas appear **idle** during the load test "
            f"(primary peaked at ~{primary_max_m}m CPU, replicas at ~{replica_max_m}m). "
            "The application code is not routing reads to `DB_READ_HOST`. "
            "Bumping `database.replicas` further will not help until the code "
            "uses the read pool (consider a `code` refinement)."
        )
    return ""


def collect_iteration_feedback(
    *,
    perf_run_dir: Path,
    iteration_path: Path,
    logger: logging.Logger | None = None,
) -> IterationFeedback:
    bench_log_path = perf_run_dir / "bench.log"
    bench_log = ""
    if bench_log_path.is_file():
        bench_log = bench_log_path.read_text(encoding="utf-8", errors="replace")

    spec: K8sWorkloadSpec | None = None
    spec_path = find_iteration_spec_path(iteration_path)
    if spec_path is not None and spec_path.is_file():
        try:
            spec = K8sWorkloadSpec.from_yaml_file(spec_path)
        except ValueError:
            spec = None

    run_config = read_run_config(perf_run_dir)
    max_connections = None
    if spec is not None:
        max_connections = spec.database.max_connections
    elif run_config.get("k8s_workload_spec"):
        try:
            max_connections = int(
                (run_config["k8s_workload_spec"].get("database") or {}).get(
                    "max_connections"
                )
            )
        except (TypeError, ValueError):
            max_connections = None

    diagnostics = summarize_run_dir(
        perf_run_dir,
        bench_log=bench_log,
        max_connections=max_connections,
    )
    notes = _replica_usage_note(diagnostics.utilization, spec)

    sustained_gp = None
    sustained_users = None
    try:
        from .plots.ramp_data import sustained_goodput_from_bench

        sustained = sustained_goodput_from_bench(perf_run_dir, log_text=bench_log)
        if sustained is not None:
            sustained_gp = float(sustained.goodput_rps)
            sustained_users = sustained.users
    except Exception:
        pass

    return IterationFeedback(
        iteration_id=iteration_path.name,
        perf_run_dir=str(perf_run_dir),
        locust_summary=_locust_summary_from_run_dir(perf_run_dir, bench_log),
        error_excerpt=_extract_error_excerpt(bench_log),
        load_run_summary=summarize_load_run(
            bench_log,
            sustained_goodput_rps=sustained_gp,
            sustained_users=sustained_users,
        ),
        diagnostics_summary=diagnostics.to_prompt_block(),
        notes=notes,
    )


# Note: persistence + loading of ``iteration_feedback.json`` now lives in
# ``workspace.artifacts`` (``write_feedback`` / ``load_feedback``). This module
# is the *builder* (parses Locust CSVs + diagnostics into ``IterationFeedback``);


def read_failed_iteration_error_excerpt(
    iteration_path: Path,
    *,
    max_chars: int = 1200,
) -> str:
    """
    Pull a short error excerpt from the most relevant artifact of a failed iteration.

    Preference order:

    1. **Structured ``failure.json``** (terminal :class:`IterationFailure` on disk).
    2. Legacy ``failure_report.json`` (v1 code failures).
    3. ``bench/bench.log`` — last 40 lines for spec-refinement failures.
    3. ``04-deploy/probe.json`` — full content for failed deploy probes.
    4. ``functional_tests/test.log`` — last-resort tail; almost always the
       *last* test (often a passing one) and therefore mostly noise. Kept only
       so older iterations without a ``failure_report.json`` still produce
       something non-empty.
    """
    try:
        from .failure import load_terminal_failure_record

        record = load_terminal_failure_record(iteration_path)
    except Exception:
        record = None
    if record is not None:
        return record.short_excerpt()[:max_chars]

    bench_log = iteration_bench_dir(iteration_path) / "bench.log"
    if bench_log.is_file():
        try:
            text = bench_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            tail = "\n".join(text.splitlines()[-40:])
            return tail[-max_chars:]

    probe = deploy_probe_record_path(iteration_path)
    if probe.is_file():
        try:
            return probe.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            pass

    test_log = iteration_functional_tests_dir(iteration_path) / "test.log"
    if test_log.is_file():
        try:
            text = test_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            tail = "\n".join(text.splitlines()[-40:])
            return tail[-max_chars:]
    return ""


def load_prior_feedback_for_iteration(
    sample_dir: Path,
    iteration_index: int,
    *,
    experiment_id: str | None = None,
) -> IterationFeedback | None:
    """
    Load bench feedback from iteration **N−1 only** when that iteration
    completed a successful load run.

    Returns ``None`` when N−1 failed before bench or produced no feedback.
    Older successful benches are not re-inlined — they remain in conversation
    history from the decision turn that originally consumed them.

    When N−1 failed, use :func:`k8s_bench.failure.load_prior_iteration_failure`
    for the structured failure envelope from that immediate predecessor.
    """
    if iteration_index <= 0:
        return None

    from .workspace import (
        iteration_id_for_index,
        iteration_is_failed,
        load_feedback,
        resolve_bench_dir,
        resolve_iteration_dir,
    )

    prev_id = iteration_id_for_index(iteration_index - 1)
    ip = resolve_iteration_dir(
        sample_dir, prev_id, experiment_id=experiment_id, exclude_failed=False
    )
    if iteration_is_failed(ip):
        return None

    bench = resolve_bench_dir(
        sample_dir, prev_id, experiment_id=experiment_id
    )
    if bench is None:
        return None
    fb = load_feedback(bench)
    if fb is None:
        fb = collect_iteration_feedback(
            perf_run_dir=bench,
            iteration_path=ip,
        )
    return fb

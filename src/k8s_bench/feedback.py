"""
Collect benchmark feedback for the next K8s spec-generation iteration.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec.models import K8sWorkloadSpec
from .cluster.preflight import _kubectl
from .workspace import (
    deploy_probe_record_path,
    find_iteration_spec_path,
    iteration_bench_dir,
    iteration_functional_tests_dir,
)


@dataclass(frozen=True)
class FailedAttempt:
    """One iteration that ran between the last successful bench and the current phase."""

    iteration_id: str
    kind: str
    refinement_action: str | None
    rationale: str | None
    failure_reason: str
    error_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration_id": self.iteration_id,
            "kind": self.kind,
            "refinement_action": self.refinement_action,
            "rationale": self.rationale,
            "failure_reason": self.failure_reason,
            "error_excerpt": self.error_excerpt,
        }

    def to_prompt_block(self) -> str:
        lines = [
            f"- Iteration `{self.iteration_id}` (kind={self.kind}"
            + (f", action={self.refinement_action}" if self.refinement_action else "")
            + ")",
            f"  - Failure reason: {self.failure_reason or '(unspecified)'}",
        ]
        if self.rationale:
            rationale = self.rationale.strip().replace("\n", " ")
            if len(rationale) > 400:
                rationale = rationale[:400] + "…"
            lines.append(f"  - Rationale at the time: {rationale}")
        if self.error_excerpt:
            excerpt = self.error_excerpt.strip()
            if len(excerpt) > 800:
                excerpt = excerpt[:800] + "\n…(truncated)"
            lines.extend(["  - Error excerpt:", "    ```", excerpt, "    ```"])
        return "\n".join(lines)


@dataclass(frozen=True)
class IterationFeedback:
    """
    Structured summary of one prior iteration.

    A feedback object can describe either a **successful** iteration (full
    Locust + kubectl data) or a **failed** one (no bench, no k8s metrics, just
    the failure reason + an error excerpt + the spec that was attempted). The
    ``status`` field distinguishes the two; the renderer adapts.
    """

    iteration_id: str
    perf_run_dir: str
    locust_summary: str
    error_excerpt: str
    pod_utilization: str
    previous_spec_yaml: str
    notes: str = ""
    failed_attempts: tuple[FailedAttempt, ...] = field(default_factory=tuple)
    status: str = "success"  # "success" | "failed"
    failure_reason: str = ""
    failure_kind: str = ""  # "spec" | "code" | "baseline" | "" for success
    decision_rationale: str = ""

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def to_prompt_text(self) -> str:
        if self.is_failed:
            return self._to_prompt_text_failed()
        return self._to_prompt_text_success()

    def _to_prompt_text_success(self) -> str:
        parts = [
            f"Most recent successful iteration: {self.iteration_id}",
            "",
            "## Locust results (per endpoint)",
            "Source: Locust ``bench_results_*_stats.csv`` (markdown table; includes p95/p99 from Locust percentiles).",
            self.locust_summary or "(no Locust stats found)",
            "",
            "## Kubernetes utilization (aggregated over benchmark run)",
            "From periodic ``kubectl top`` samples during the run (min / avg / max per pod and per node).",
            self.pod_utilization or "(kubernetes metrics unavailable)",
            "",
            "## Top errors",
            self.error_excerpt or "(no error report)",
            "",
            "## Previous spec.yaml",
            "```yaml",
            self.previous_spec_yaml.strip() or "(missing)",
            "```",
        ]
        if self.failed_attempts:
            parts.extend(
                [
                    "",
                    "## Failed attempts since this last successful iteration",
                    (
                        "The iterations below were attempted after "
                        f"`{self.iteration_id}` but did not produce benchmark "
                        "results. Treat them as **anti-examples**: do not repeat "
                        "the same change, and either fix the underlying cause or "
                        "try a different direction."
                    ),
                    "",
                    *[fa.to_prompt_block() for fa in self.failed_attempts],
                ]
            )
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
            "iteration; the change below was attempted but the benchmark never "
            "ran. Read the failure reason and error excerpt, then propose a fix "
            "that addresses the *cause* — do not blindly re-apply the same "
            "change.",
            "",
            "## Failure",
            f"- **Stage**: `{kind_label}` "
            "(`baseline`/`spec` = manifest could not be deployed; "
            "`code` = functional tests did not pass after code refinement)",
            f"- **Reason**: {self.failure_reason or '(unspecified)'}",
        ]
        if self.decision_rationale:
            rationale = self.decision_rationale.strip().replace("\n", " ")
            if len(rationale) > 600:
                rationale = rationale[:600] + "…"
            parts.append(f"- **Decision at the time**: {rationale}")
        parts.extend(
            [
                "",
                "## Error excerpt",
                self.error_excerpt.strip() or "(no error excerpt captured)",
                "",
                "## Spec that was attempted",
                "```yaml",
                self.previous_spec_yaml.strip() or "(no spec.yaml on disk)",
                "```",
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
    """Format ``bench_results_*_stats.csv`` as a markdown table (LLM-friendly vs raw CSV)."""
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
    candidates = sorted(perf_run_dir.glob("bench_results_*_stats.csv"))
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


def _kubectl_top_pods(namespace: str, logger: logging.Logger) -> str:
    proc = _kubectl(
        ["top", "pods", "-n", namespace, "--no-headers"],
        timeout_s=30,
    )
    if proc.returncode != 0:
        logger.debug(
            "kubectl top pods failed (metrics-server may be missing): %s",
            (proc.stderr or proc.stdout or "").strip()[:200],
        )
        return ""
    return (proc.stdout or "").strip()


def _parse_cpu_millicores(value: str) -> int | None:
    v = (value or "").strip()
    if not v or v == "<unknown>":
        return None
    if v.endswith("m"):
        return int(round(float(v[:-1])))
    try:
        return int(round(float(v) * 1000))
    except ValueError:
        return None


def _parse_memory_mi(value: str) -> int | None:
    v = (value or "").strip()
    if not v or v == "<unknown>":
        return None
    if v.endswith("Mi"):
        return int(round(float(v[:-2])))
    if v.endswith("Gi"):
        return int(round(float(v[:-2]) * 1024))
    if v.endswith("Ki"):
        return int(round(float(v[:-2]) / 1024))
    return None


def _parse_pct(value: str) -> float | None:
    v = (value or "").strip().rstrip("%")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _min_avg_max(values: list[int]) -> str:
    if not values:
        return "-"
    return f"{min(values)}/{int(round(sum(values) / len(values)))}/{max(values)}"


def _min_avg_max_f(values: list[float]) -> str:
    if not values:
        return "-"
    return (
        f"{min(values):.0f}/{sum(values) / len(values):.0f}/{max(values):.0f}"
    )


def _summarize_pod_top_csv(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    cpu_by_pod: dict[str, list[int]] = defaultdict(list)
    mem_by_pod: dict[str, list[int]] = defaultdict(list)

    for row in rows:
        pod = (row.get("pod") or "").strip()
        if not pod:
            continue
        cpu = _parse_cpu_millicores(row.get("cpu") or "")
        mem = _parse_memory_mi(row.get("memory") or "")
        if cpu is not None:
            cpu_by_pod[pod].append(cpu)
        if mem is not None:
            mem_by_pod[pod].append(mem)

    pod_names = sorted(cpu_by_pod.keys() | mem_by_pod.keys())
    lines = [
        f"kubectl top pods: {samples} sample(s), {len(pod_names)} pod(s)",
        "",
        "| Pod | CPU m (min/avg/max) | Memory Mi (min/avg/max) |",
        "|---|---:|---:|",
    ]
    for pod in pod_names:
        lines.append(
            f"| {pod} | {_min_avg_max(cpu_by_pod[pod])} | "
            f"{_min_avg_max(mem_by_pod[pod])} |"
        )
    return "\n".join(lines)


def _summarize_node_top_csv(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    cpu_by_node: dict[str, list[int]] = defaultdict(list)
    cpu_pct_by_node: dict[str, list[float]] = defaultdict(list)
    mem_mi_by_node: dict[str, list[int]] = defaultdict(list)
    mem_pct_by_node: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        node = (row.get("node") or "").strip()
        if not node:
            continue
        short = node.split(".", 1)[0]
        cpu = _parse_cpu_millicores(row.get("cpu") or "")
        if cpu is not None:
            cpu_by_node[short].append(cpu)
        pct = _parse_pct(row.get("cpu_pct") or "")
        if pct is not None:
            cpu_pct_by_node[short].append(pct)
        mem = _parse_memory_mi(row.get("memory") or "")
        if mem is not None:
            mem_mi_by_node[short].append(mem)
        mpct = _parse_pct(row.get("memory_pct") or "")
        if mpct is not None:
            mem_pct_by_node[short].append(mpct)

    lines = [
        f"kubectl top nodes: {samples} sample(s)",
        "",
        "| Node | CPU m (min/avg/max) | CPU % (min/avg/max) | "
        "Memory Mi (min/avg/max) | Memory % (min/avg/max) |",
        "|---|---:|---:|---:|---:|",
    ]
    for node in sorted(cpu_by_node.keys()):
        lines.append(
            f"| {node} | {_min_avg_max(cpu_by_node[node])} | "
            f"{_min_avg_max_f(cpu_pct_by_node[node])} | "
            f"{_min_avg_max(mem_mi_by_node[node])} | "
            f"{_min_avg_max_f(mem_pct_by_node[node])} |"
        )
    return "\n".join(lines)


def _summarize_k8s_utilization_csv(perf_run_dir: Path) -> str:
    """Aggregate ``stats/kubernetes/*.csv`` over the whole perf run."""
    k8s_dir = perf_run_dir / "stats" / "kubernetes"
    pod_csv = k8s_dir / "pod_top.csv"
    node_csv = k8s_dir / "node_top.csv"
    parts: list[str] = []

    pod_block = _summarize_pod_top_csv(pod_csv)
    if pod_block:
        parts.append("### Pods")
        parts.append(pod_block)

    node_block = _summarize_node_top_csv(node_csv)
    if node_block:
        parts.append("### Nodes")
        parts.append(node_block)

    return "\n\n".join(parts)


def _replica_usage_note(
    pod_util_text: str,
    spec: K8sWorkloadSpec | None,
) -> str:
    """
    Flag the common pitfall: spec deployed read replicas but the app code only
    talks to the primary (replicas idle while primary saturates).
    """
    if spec is None or not spec.database.enabled or spec.database.replicas <= 1:
        return ""
    if not pod_util_text:
        return ""

    primary_max_m: int | None = None
    replica_max_m: int | None = None
    for line in pod_util_text.splitlines():
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
    namespace: str | None = None,
    logger: logging.Logger | None = None,
) -> IterationFeedback:
    log = logger or logging.getLogger(__name__)
    bench_log_path = perf_run_dir / "bench.log"
    bench_log = ""
    if bench_log_path.is_file():
        bench_log = bench_log_path.read_text(encoding="utf-8", errors="replace")

    spec_yaml = ""
    spec: K8sWorkloadSpec | None = None
    spec_path = find_iteration_spec_path(iteration_path)
    if spec_path is not None and spec_path.is_file():
        spec_yaml = spec_path.read_text(encoding="utf-8", errors="replace")
        try:
            spec = K8sWorkloadSpec.from_yaml_file(spec_path)
        except ValueError:
            spec = None

    ns = namespace
    if not ns:
        cfg_path = perf_run_dir / "config.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                ns = (cfg.get("k8s_iteration") or {}).get("namespace")
            except json.JSONDecodeError:
                pass
    if not ns and spec is not None:
        ns = spec.namespace

    k8s_util = _summarize_k8s_utilization_csv(perf_run_dir)
    if not k8s_util and ns:
        k8s_util = _kubectl_top_pods(ns, log)

    notes = _replica_usage_note(k8s_util, spec)

    return IterationFeedback(
        iteration_id=iteration_path.name,
        perf_run_dir=str(perf_run_dir),
        locust_summary=_locust_summary_from_run_dir(perf_run_dir, bench_log),
        error_excerpt=_extract_error_excerpt(bench_log),
        pod_utilization=k8s_util,
        previous_spec_yaml=spec_yaml,
        notes=notes,
    )


# Note: persistence + loading of ``iteration_feedback.json`` now lives in
# ``workspace.artifacts`` (``write_feedback`` / ``load_feedback``). This module
# is the *builder* (parses Locust CSVs + kubectl + logs into
# ``IterationFeedback``); the filesystem is owned by ``workspace``.


def read_failed_iteration_error_excerpt(
    iteration_path: Path,
    *,
    max_chars: int = 1200,
) -> str:
    """
    Pull a short error excerpt from the most relevant artifact of a failed iteration.

    Preference order:

    1. **Structured ``failure_report.json``** (written by
       :func:`fail_iteration_phase` when functional tests failed). This is a
       one-paragraph summary like
       ``"Functional tests: 4/5 passed. Failed: func_test_simulate_… First
       failure evidence: …"`` — far more useful than a random log tail and
       does not change across reruns.
    2. ``bench/bench.log`` — last 40 lines for spec-refinement failures.
    3. ``deploy/probe.json`` — full content for failed deploy probes.
    4. ``functional_tests/test.log`` — last-resort tail; almost always the
       *last* test (often a passing one) and therefore mostly noise. Kept only
       so older iterations without a ``failure_report.json`` still produce
       something non-empty.
    """
    try:
        from .workspace import load_failure_report

        report = load_failure_report(iteration_path)
    except Exception:
        report = None
    if report is not None and (report.failed_tests or report.num_total_ft > 0):
        return report.short_excerpt()[:max_chars]

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
) -> IterationFeedback | None:
    """
    Load feedback from the **immediately preceding** iteration (``iteration_index - 1``).

    - If the previous iteration *succeeded*, return its bench-derived
      :class:`IterationFeedback` as before.
    - If it *failed*, return a feedback object with ``status="failed"``,
      empty Locust / k8s sections, and the failure narrative (reason +
      error excerpt + the spec that was attempted). The renderer in
      :meth:`IterationFeedback.to_prompt_text` adapts.

    The loader deliberately does **not** walk further back: if iteration N-1
    failed, the next iteration's prompt anchors on N-1's failure (not on
    some older successful run), so the LLM has direct context for what to
    fix without us digging up stale bench data.
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

    prev_idx = iteration_index - 1
    prev_id = iteration_id_for_index(prev_idx)
    ip = resolve_iteration_dir(sample_dir, prev_id)

    if iteration_is_failed(ip):
        return _failed_iteration_feedback(ip, prev_id)

    bench = resolve_bench_dir(sample_dir, prev_id)
    if bench is None:
        return None
    fb = load_feedback(bench)
    if fb is None:
        fb = collect_iteration_feedback(
            perf_run_dir=bench,
            iteration_path=ip,
        )
    return fb


def _failed_iteration_feedback(
    iteration_path: Path,
    iteration_id: str,
) -> IterationFeedback:
    """Build :class:`IterationFeedback` for a prior iteration that failed before bench."""
    from .workspace import read_decision_rationale, read_iteration_meta

    meta = read_iteration_meta(iteration_path) or {}
    failure_reason = str(meta.get("failure_reason") or "").strip()
    raw_kind = meta.get("refinement_action") or ""
    kind = str(raw_kind) if raw_kind in {"code", "spec", "baseline"} else ""
    rationale = read_decision_rationale(iteration_path) or ""
    excerpt = read_failed_iteration_error_excerpt(iteration_path)
    spec_path = find_iteration_spec_path(iteration_path)
    attempted_spec = ""
    if spec_path is not None and spec_path.is_file():
        try:
            attempted_spec = spec_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            attempted_spec = ""
    return IterationFeedback(
        iteration_id=str(meta.get("iteration_id") or iteration_id),
        perf_run_dir="",
        locust_summary="",
        error_excerpt=excerpt,
        pod_utilization="",
        previous_spec_yaml=attempted_spec,
        notes="",
        failed_attempts=(),
        status="failed",
        failure_reason=failure_reason or "(no reason recorded)",
        failure_kind=kind,
        decision_rationale=rationale,
    )

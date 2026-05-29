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
from .workspace import find_iteration_spec_path


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
    """Structured summary of one completed perf-k8s run."""

    iteration_id: str
    perf_run_dir: str
    locust_summary: str
    error_excerpt: str
    pod_utilization: str
    previous_spec_yaml: str
    notes: str = ""
    failed_attempts: tuple[FailedAttempt, ...] = field(default_factory=tuple)

    def to_prompt_text(self) -> str:
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


def write_feedback_artifact(
    perf_run_dir: Path,
    feedback: IterationFeedback,
) -> Path:
    """Persist feedback next to bench.log for inspection and later phases."""
    out = perf_run_dir / "iteration_feedback.json"
    prompt_text = feedback.to_prompt_text()
    payload: dict[str, Any] = {
        "iteration_id": feedback.iteration_id,
        "perf_run_dir": feedback.perf_run_dir,
        "locust_summary": feedback.locust_summary,
        "error_excerpt": feedback.error_excerpt,
        "pod_utilization": feedback.pod_utilization,
        "previous_spec_yaml": feedback.previous_spec_yaml,
        "notes": feedback.notes,
        "failed_attempts": [fa.to_dict() for fa in feedback.failed_attempts],
        "prompt_text": prompt_text,
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (perf_run_dir / "iteration_feedback.txt").write_text(prompt_text + "\n", encoding="utf-8")
    return out


def _failed_attempts_from_payload(
    data: dict[str, Any],
) -> tuple[FailedAttempt, ...]:
    raw = data.get("failed_attempts") or []
    if not isinstance(raw, list):
        return ()
    out: list[FailedAttempt] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(
            FailedAttempt(
                iteration_id=str(entry.get("iteration_id", "")),
                kind=str(entry.get("kind", "")),
                refinement_action=(
                    str(entry["refinement_action"])
                    if entry.get("refinement_action")
                    else None
                ),
                rationale=(
                    str(entry["rationale"]) if entry.get("rationale") else None
                ),
                failure_reason=str(entry.get("failure_reason", "")),
                error_excerpt=str(entry.get("error_excerpt", "")),
            )
        )
    return tuple(out)


def load_feedback_from_run_dir(perf_run_dir: Path) -> IterationFeedback | None:
    json_path = perf_run_dir / "iteration_feedback.json"
    if json_path.is_file():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return IterationFeedback(
            iteration_id=str(data.get("iteration_id", "")),
            perf_run_dir=str(data.get("perf_run_dir", perf_run_dir)),
            locust_summary=str(data.get("locust_summary", "")),
            error_excerpt=str(data.get("error_excerpt", "")),
            pod_utilization=str(data.get("pod_utilization", "")),
            previous_spec_yaml=str(data.get("previous_spec_yaml", "")),
            notes=str(data.get("notes", "")),
            failed_attempts=_failed_attempts_from_payload(data),
        )
    txt = perf_run_dir / "iteration_feedback.txt"
    if txt.is_file():
        return IterationFeedback(
            iteration_id=perf_run_dir.name,
            perf_run_dir=str(perf_run_dir),
            locust_summary="",
            error_excerpt="",
            pod_utilization="",
            previous_spec_yaml="",
            notes=txt.read_text(encoding="utf-8", errors="replace"),
        )
    cfg_path = perf_run_dir / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            iter_path = (cfg.get("k8s_iteration") or {}).get("path")
            if iter_path:
                return collect_iteration_feedback(
                    perf_run_dir=perf_run_dir,
                    iteration_path=Path(iter_path),
                )
        except json.JSONDecodeError:
            pass
    return None


def _read_decision_rationale(iteration_path: Path) -> str | None:
    decision_path = iteration_path / "decision" / "decision.json"
    if not decision_path.is_file():
        return None
    try:
        data = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rationale = data.get("rationale")
    return str(rationale).strip() if rationale else None


def _read_failed_iteration_error_excerpt(
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
        from .functional_failure import load_failure_report

        report = load_failure_report(iteration_path)
    except Exception:
        report = None
    if report is not None and (report.failed_tests or report.num_total_ft > 0):
        return report.short_excerpt()[:max_chars]

    bench_log = iteration_path / "bench" / "bench.log"
    if bench_log.is_file():
        try:
            text = bench_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            tail = "\n".join(text.splitlines()[-40:])
            return tail[-max_chars:]

    probe = iteration_path / "deploy" / "probe.json"
    if probe.is_file():
        try:
            return probe.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            pass

    test_log = iteration_path / "functional_tests" / "test.log"
    if test_log.is_file():
        try:
            text = test_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            tail = "\n".join(text.splitlines()[-40:])
            return tail[-max_chars:]
    return ""


def _failed_attempt_from_iteration(
    iteration_path: Path,
) -> FailedAttempt | None:
    """Build a ``FailedAttempt`` from a failed iteration directory."""
    from .workspace import read_iteration_meta

    meta = read_iteration_meta(iteration_path)
    if not meta:
        return None
    failure_reason = str(meta.get("failure_reason") or "").strip()
    refinement_action = meta.get("refinement_action")
    kind = refinement_action or "iteration"
    rationale = _read_decision_rationale(iteration_path)
    excerpt = _read_failed_iteration_error_excerpt(iteration_path)
    return FailedAttempt(
        iteration_id=str(meta.get("iteration_id") or iteration_path.name),
        kind=str(kind),
        refinement_action=(
            str(refinement_action) if refinement_action else None
        ),
        rationale=rationale,
        failure_reason=failure_reason or "(no reason recorded)",
        error_excerpt=excerpt,
    )


def load_prior_feedback_for_phase(
    sample_dir: Path,
    phase_index: int,
) -> IterationFeedback | None:
    """
    Load feedback from the most recent successful iteration before ``phase_index``,
    annotated with any failed attempts that came after it.

    Failed iterations are NOT skipped silently: each one is appended as a
    ``FailedAttempt`` so the next prompt knows what was tried and why it failed.
    """
    if phase_index <= 0:
        return None

    from .workspace import (
        iteration_id_for_phase,
        iteration_is_failed,
        resolve_bench_dir,
        resolve_iteration_dir,
    )

    failed_attempts: list[FailedAttempt] = []
    base_feedback: IterationFeedback | None = None

    for prev_phase in range(phase_index - 1, -1, -1):
        prev_id = iteration_id_for_phase(prev_phase)
        ip = resolve_iteration_dir(sample_dir, prev_id)
        if iteration_is_failed(ip):
            attempt = _failed_attempt_from_iteration(ip)
            if attempt is not None:
                failed_attempts.append(attempt)
            continue
        bench = resolve_bench_dir(sample_dir, prev_id)
        if bench is None:
            continue
        fb = load_feedback_from_run_dir(bench)
        if fb is None:
            fb = collect_iteration_feedback(
                perf_run_dir=bench,
                iteration_path=ip,
            )
        if fb is not None:
            base_feedback = fb
            break

    if base_feedback is None and not failed_attempts:
        return None

    # Failed attempts are collected newest-first; reverse to chronological order
    # so the prompt reads from the oldest failed attempt to the most recent.
    ordered_attempts = tuple(reversed(failed_attempts))

    if base_feedback is None:
        # No successful prior; surface only the failure stack so the next phase
        # still has context (avoids re-running the same broken change blindly).
        return IterationFeedback(
            iteration_id="(no successful iteration yet)",
            perf_run_dir="",
            locust_summary="",
            error_excerpt="",
            pod_utilization="",
            previous_spec_yaml="",
            notes=(
                "No successful iteration produced benchmark data yet. "
                "Only the failed attempts below are available as context."
            ),
            failed_attempts=ordered_attempts,
        )

    if not ordered_attempts:
        return base_feedback

    return IterationFeedback(
        iteration_id=base_feedback.iteration_id,
        perf_run_dir=base_feedback.perf_run_dir,
        locust_summary=base_feedback.locust_summary,
        error_excerpt=base_feedback.error_excerpt,
        pod_utilization=base_feedback.pod_utilization,
        previous_spec_yaml=base_feedback.previous_spec_yaml,
        notes=base_feedback.notes,
        failed_attempts=ordered_attempts,
    )

"""
Collect benchmark feedback for the next K8s spec-generation iteration.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .spec.models import K8sWorkloadSpec
from .cluster.preflight import _kubectl


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

    def to_prompt_text(self) -> str:
        parts = [
            f"Previous iteration: {self.iteration_id}",
            f"Perf run directory: {self.perf_run_dir}",
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
    spec_path = iteration_path / "spec.yaml"
    if spec_path.is_file():
        spec_yaml = spec_path.read_text(encoding="utf-8", errors="replace")

    ns = namespace
    if not ns:
        cfg_path = perf_run_dir / "config.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                ns = (cfg.get("k8s_iteration") or {}).get("namespace")
            except json.JSONDecodeError:
                pass
    if not ns and spec_path.is_file():
        try:
            ns = K8sWorkloadSpec.from_yaml_file(spec_path).namespace
        except ValueError:
            pass

    k8s_util = _summarize_k8s_utilization_csv(perf_run_dir)
    if not k8s_util and ns:
        k8s_util = _kubectl_top_pods(ns, log)

    return IterationFeedback(
        iteration_id=iteration_path.name,
        perf_run_dir=str(perf_run_dir),
        locust_summary=_locust_summary_from_run_dir(perf_run_dir, bench_log),
        error_excerpt=_extract_error_excerpt(bench_log),
        pod_utilization=k8s_util,
        previous_spec_yaml=spec_yaml,
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
        "prompt_text": prompt_text,
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (perf_run_dir / "iteration_feedback.txt").write_text(prompt_text + "\n", encoding="utf-8")
    return out


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

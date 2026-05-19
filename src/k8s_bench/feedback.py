"""
Collect benchmark feedback for the next K8s spec-generation iteration.
"""

from __future__ import annotations

import json
import logging
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
            "## Locust results (summary)",
            self.locust_summary or "(no Locust summary found in bench.log)",
            "",
            "## Top errors",
            self.error_excerpt or "(no error report found)",
            "",
            "## Pod utilization (kubectl top, if available)",
            self.pod_utilization or "(pod metrics unavailable)",
            "",
            "## Previous spec.yaml",
            "```yaml",
            self.previous_spec_yaml.strip() or "(missing)",
            "```",
        ]
        if self.notes:
            parts.extend(["", "## Notes", self.notes])
        return "\n".join(parts)


def _extract_locust_summary(bench_log: str) -> str:
    lines: list[str] = []
    in_stats = False
    for line in bench_log.splitlines():
        if line.strip().startswith("Aggregated"):
            lines.append(line.strip())
            in_stats = True
        elif in_stats and line.strip().startswith("Type"):
            lines.append(line.strip())
        elif in_stats and line.strip().startswith("----"):
            continue
        elif in_stats and "req/s" in line and len(lines) < 12:
            lines.append(line.strip())
        elif in_stats and line.strip().startswith("Response time"):
            break
    return "\n".join(lines)


def _extract_error_excerpt(bench_log: str, *, max_lines: int = 25) -> str:
    marker = "Error report"
    idx = bench_log.find(marker)
    if idx < 0:
        return ""
    tail = bench_log[idx:].splitlines()[1 : max_lines + 1]
    return "\n".join(ln.rstrip() for ln in tail if ln.strip())


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

    pod_top = _kubectl_top_pods(ns, log) if ns else ""

    return IterationFeedback(
        iteration_id=iteration_path.name,
        perf_run_dir=str(perf_run_dir),
        locust_summary=_extract_locust_summary(bench_log),
        error_excerpt=_extract_error_excerpt(bench_log),
        pod_utilization=pod_top,
        previous_spec_yaml=spec_yaml,
    )


def write_feedback_artifact(
    perf_run_dir: Path,
    feedback: IterationFeedback,
) -> Path:
    """Persist feedback next to bench.log for inspection and later phases."""
    out = perf_run_dir / "iteration_feedback.json"
    payload: dict[str, Any] = {
        "iteration_id": feedback.iteration_id,
        "perf_run_dir": feedback.perf_run_dir,
        "locust_summary": feedback.locust_summary,
        "error_excerpt": feedback.error_excerpt,
        "pod_utilization": feedback.pod_utilization,
        "previous_spec_yaml": feedback.previous_spec_yaml,
        "notes": feedback.notes,
        "prompt_text": feedback.to_prompt_text(),
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (perf_run_dir / "iteration_feedback.txt").write_text(
        feedback.to_prompt_text() + "\n",
        encoding="utf-8",
    )
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

"""
Filesystem I/O for per-iteration artifacts.

Workspace owns *where* artifacts live on disk and *how* they're serialized. The
data classes themselves live with their builders/parsers:

- ``IterationFeedback``  → ``feedback.py``           (Locust + kubectl parsing)
- ``FunctionalFailureReport`` → ``failure/`` (FT log parsing)
- ``RefinementDecision`` → ``stages/decision.py`` (LLM decision)

This module is the single place that reads/writes the JSON envelopes so other
modules never touch the filesystem directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .paths import iteration_code_phase_dir, iteration_decision_dir

if TYPE_CHECKING:
    from ..feedback import IterationFeedback
    from ..failure import FunctionalFailureReport
    from ..stages.decision import RefinementDecision


FEEDBACK_FILENAME = "iteration_feedback.json"
FEEDBACK_TEXT_FILENAME = "iteration_feedback.txt"
FAILURE_REPORT_FILENAME = "failure_report.json"
DECISION_FILENAME = "decision.json"

# Every LLM-driven phase folder (``01-decision/``, ``02-code/``, ``03-spec/``)
# writes the exact prompt sent to the model and the exact raw response back to
# the same two filenames, so a human reading the iteration can audit the LLM
# transcript without having to know which phase used which legacy filename.
PROMPT_LOG_FILENAME = "prompt.log"
RESPONSE_LOG_FILENAME = "response.log"


def feedback_artifact_path(perf_run_dir: Path) -> Path:
    return perf_run_dir / FEEDBACK_FILENAME


def failure_report_path(iteration_path: Path) -> Path:
    """
    Canonical path for the structured FT failure report.

    The report describes a failure in the *code regeneration* phase, so it
    lives next to the regenerated code under ``02-code/failure_report.json``.
    """
    return iteration_code_phase_dir(iteration_path) / FAILURE_REPORT_FILENAME


def decision_artifact_path(iteration_path: Path) -> Path:
    return iteration_decision_dir(iteration_path) / DECISION_FILENAME


def write_feedback(
    perf_run_dir: Path,
    feedback: "IterationFeedback",
) -> Path:
    """Persist :class:`IterationFeedback` next to ``bench.log``."""
    out = feedback_artifact_path(perf_run_dir)
    prompt_text = feedback.to_prompt_text()
    payload: dict[str, Any] = {
        "iteration_id": feedback.iteration_id,
        "perf_run_dir": feedback.perf_run_dir,
        "locust_summary": feedback.locust_summary,
        "error_excerpt": feedback.error_excerpt,
        "pod_utilization": feedback.pod_utilization,
        "benchmark_context": feedback.benchmark_context,
        "load_run_summary": feedback.load_run_summary,
        "diagnostics_summary": feedback.diagnostics_summary,
        "notes": feedback.notes,
        "status": feedback.status,
        "failure_reason": feedback.failure_reason,
        "failure_kind": feedback.failure_kind,
        "decision_rationale": feedback.decision_rationale,
        "prompt_text": prompt_text,
    }
    out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (perf_run_dir / FEEDBACK_TEXT_FILENAME).write_text(
        prompt_text + "\n", encoding="utf-8"
    )
    return out


def load_feedback(perf_run_dir: Path) -> "IterationFeedback | None":
    """Load :class:`IterationFeedback` from a bench run directory, if present."""
    from ..feedback import IterationFeedback, collect_iteration_feedback

    json_path = feedback_artifact_path(perf_run_dir)
    if json_path.is_file():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        fb = IterationFeedback(
            iteration_id=str(data.get("iteration_id", "")),
            perf_run_dir=str(data.get("perf_run_dir", perf_run_dir)),
            locust_summary=str(data.get("locust_summary", "")),
            error_excerpt=str(data.get("error_excerpt", "")),
            pod_utilization=str(data.get("pod_utilization", "")),
            benchmark_context=str(data.get("benchmark_context", "")),
            load_run_summary=str(data.get("load_run_summary", "")),
            diagnostics_summary=str(data.get("diagnostics_summary", "")),
            notes=str(data.get("notes", "")),
            status=str(data.get("status", "success")),
            failure_reason=str(data.get("failure_reason", "")),
            failure_kind=str(data.get("failure_kind", "")),
            decision_rationale=str(data.get("decision_rationale", "")),
        )
        if (
            fb.status == "success"
            and not fb.diagnostics_summary.strip()
            and (perf_run_dir / "diagnostics" / "kubernetes").is_dir()
        ):
            try:
                cfg = json.loads((perf_run_dir / "config.json").read_text(encoding="utf-8"))
                iter_path = Path((cfg.get("k8s_iteration") or {}).get("path", ""))
                if iter_path.is_dir():
                    return collect_iteration_feedback(
                        perf_run_dir=perf_run_dir,
                        iteration_path=iter_path,
                    )
            except (json.JSONDecodeError, OSError):
                pass
        return fb
    txt = perf_run_dir / FEEDBACK_TEXT_FILENAME
    if txt.is_file():
        return IterationFeedback(
            iteration_id=perf_run_dir.name,
            perf_run_dir=str(perf_run_dir),
            locust_summary="",
            error_excerpt="",
            pod_utilization="",
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


def write_failure_report(
    iteration_path: Path,
    report: "FunctionalFailureReport",
) -> Path:
    """Persist :class:`FunctionalFailureReport` next to ``meta.json``."""
    out = failure_report_path(iteration_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def load_failure_report(
    iteration_path: Path,
) -> "FunctionalFailureReport | None":
    """Load :class:`FunctionalFailureReport` from a failed iteration directory."""
    from ..failure import FunctionalFailureReport

    path = failure_report_path(iteration_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return FunctionalFailureReport.from_dict(data)


def write_decision(
    iteration_path: Path,
    decision: "RefinementDecision",
) -> Path:
    """Persist :class:`RefinementDecision` + its raw LLM response."""
    decision_dir = iteration_decision_dir(iteration_path)
    decision_dir.mkdir(parents=True, exist_ok=True)
    out = decision_dir / DECISION_FILENAME
    out.write_text(
        json.dumps(decision.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (decision_dir / RESPONSE_LOG_FILENAME).write_text(
        decision.raw_response + "\n", encoding="utf-8"
    )
    return out


def read_decision_rationale(iteration_path: Path) -> str | None:
    """Return ``rationale`` from a previously written ``decision.json``, if any."""
    path = decision_artifact_path(iteration_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rationale = data.get("rationale") if isinstance(data, dict) else None
    return str(rationale) if rationale else None

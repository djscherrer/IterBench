"""
Rolling high-level summary for one k8s experiment workspace.

Appends to ``experiment_summary.md`` at the workspace root (``sampleN/`` or
``sampleN/k8s-experiments/<slug>/``) after each spec generation and each Locust run.
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_diagnostics.summary.adaptive_log import (
    ADAPTIVE_PHASE_RE as _ADAPTIVE_PHASE_RE,
    ADAPTIVE_V2_STOP_RE as _ADAPTIVE_V2_STOP_RE,
    phase_fail_pct as _phase_fail_pct,
    phase_p95_token as _phase_p95_token,
)

from .feedback import IterationFeedback
from workspace import (
    ITERATIONS_DIRNAME,
    PLOTS_DIRNAME,
    experiment_root_from_iteration_path,
    find_iteration_spec_path,
    iteration_spec_path,
    k8s_workspace_root,
    parse_iteration_folder_name,
    parse_iteration_index,
    resolve_k8s_experiment_id,
)
from .spec.models import K8sWorkloadSpec, ResourceSpec

SUMMARY_FILENAME = "experiment_summary.md"
_MAX_NARRATIVE_CHARS = 3500
_REFINEMENT_DECISION_BLOCK_RE = re.compile(
    r"^### Refinement decision[^\n]*\n.*?\n---\n", re.M | re.S
)
_CODE_REFINEMENT_BLOCK_RE = re.compile(
    r"^### Code refinement[^\n]*\n.*?\n---\n", re.M | re.S
)
_CODE_REUSE_BLOCK_RE = re.compile(r"^### Code reuse[^\n]*\n.*?\n---\n", re.M | re.S)
_STAGE_FAILURE_BLOCK_RE = re.compile(
    r"^### [^\n]+ stage failed[^\n]*\n.*?\n---\n", re.M | re.S
)
_BASELINE_CODE_FAILED_BLOCK_RE = re.compile(
    r"^### Baseline code generation failed[^\n]*\n.*?\n---\n", re.M | re.S
)
_BASELINE_CODEGEN_BLOCK_RE = re.compile(
    r"^### Baseline code generation \([^\n]*\n.*?\n---\n", re.M | re.S
)
_SPEC_GENERATION_BLOCK_RE = re.compile(
    r"^### Spec generation[^\n]*\n.*?\n---\n", re.M | re.S
)
_LOCUST_RUN_BLOCK_RE = re.compile(
    r"^### Locust run \([^)]*\)\n.*?\n---\n", re.M | re.S
)
_NEXT_ITERATION_SECTION_RE = re.compile(r"^## iteration-", re.M)
_LOG_TS_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\]"
)
_USERS_FROM_ACTION_RE = re.compile(r"users=(\d+)")
_SHAPE_UPDATE_RE = re.compile(r"Shape test updating to (\d+) users")
_ADAPTIVE_START_USERS_RE = re.compile(
    r"BAXBENCH_ADAPTIVE(?:_V2)?_START_USERS=(\d+)"
)
_STEP_GOODPUT_RE = re.compile(r"step_goodput=([\d.]+)/s")
_STEP_CV_RE = re.compile(r"\bcv=([\d.]+)")
_STEP_DRIFT_RE = re.compile(r"\bdrift=([\d.]+)%")
_SAMPLES_RE = re.compile(r"samples=\[(?P<samples>[^\]]*)\]")
_P95_SAMPLE_RE = re.compile(
    r"adaptive p95 sample t=(\d+)s users=(\d+) p95=([\d.]+)ms"
)
# Trailing Locust stats window for per-step goodput min/avg/max in the summary table.
_SUMMARY_MEASURE_WINDOW_S = 3


def _summary_section_id(iteration_path: Path) -> str:
    """Markdown section heading — matches the iteration folder name (e.g. ``iteration-003-code``)."""
    return iteration_path.name


def _iteration_folder_kind(iteration_path: Path) -> str | None:
    _idx, kind, _failed = parse_iteration_folder_name(iteration_path.name)
    return kind


def _should_record_spec_generation(iteration_path: Path) -> bool:
    """Spec blocks only for baseline / spec iterations where the LLM wrote a new spec."""
    if _iteration_folder_kind(iteration_path) == "code":
        return False
    from workspace.meta import read_iteration_meta

    if read_iteration_meta(iteration_path).get("spec_reused_from"):
        return False
    return True


def experiment_summary_path(
    sample_dir: Path, *, experiment_id: str | None = None
) -> Path:
    path = sample_dir.expanduser().resolve()
    if (path / ITERATIONS_DIRNAME).is_dir():
        return path / SUMMARY_FILENAME
    return k8s_workspace_root(path, experiment_id=experiment_id) / SUMMARY_FILENAME


def experiment_summary_path_for_iteration(iteration_path: Path) -> Path:
    """Summary file co-located with the experiment that owns ``iteration_path``."""
    return experiment_root_from_iteration_path(iteration_path) / SUMMARY_FILENAME


def _utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sample_index_from_root(experiment_root: Path) -> str | None:
    """``.../sampleN/k8s-experiments/<exp>`` → ``"N"``."""
    try:
        sample_name = experiment_root.parent.parent.name
    except Exception:
        return None
    m = re.search(r"(\d+)$", sample_name)
    return m.group(1) if m else (sample_name or None)


def _experiment_metadata(experiment_root: Path) -> dict[str, str]:
    """Best-effort model / scenario / environment / sample for the summary header.

    Prefers the baseline ``codegen.json`` (authoritative model/provider/temperature),
    then falls back to the ``results/<model>/<scenario>/<env>/<config>/sampleN`` layout.
    """
    meta: dict[str, str] = {}
    iterations_dir = experiment_root / ITERATIONS_DIRNAME
    if iterations_dir.is_dir():
        for child in sorted(iterations_dir.iterdir()):
            if not child.is_dir():
                continue
            from workspace import baseline_codegen_meta_path

            codegen_path = baseline_codegen_meta_path(child)
            if not codegen_path.is_file():
                continue
            try:
                data = json.loads(codegen_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            for key in ("model", "provider", "scenario", "env"):
                if data.get(key):
                    meta.setdefault(key, str(data[key]))
            if data.get("temperature") is not None:
                meta.setdefault("temperature", str(data["temperature"]))
            break

    # Path-based fallbacks: .../<model>/<scenario>/<env>/<config>/sampleN/k8s-experiments/<exp>
    try:
        sample_dir = experiment_root.parent.parent
        config_dir = sample_dir.parent
        meta.setdefault("env", config_dir.parent.name)
        meta.setdefault("scenario", config_dir.parent.parent.name)
        meta.setdefault("model", config_dir.parent.parent.parent.name)
    except Exception:
        pass

    sample = _sample_index_from_root(experiment_root)
    if sample is not None:
        meta.setdefault("sample", sample)
    return meta


def _header_metadata_lines(experiment_root: Path) -> list[str]:
    meta = _experiment_metadata(experiment_root)
    lines: list[str] = []
    if meta.get("model"):
        provider = meta.get("provider")
        temperature = meta.get("temperature")
        suffix = ""
        if provider or temperature is not None:
            bits = []
            if provider:
                bits.append(f"provider `{provider}`")
            if temperature is not None:
                bits.append(f"temperature {temperature}")
            suffix = " (" + ", ".join(bits) + ")"
        lines.append(f"- **Model**: `{meta['model']}`{suffix}")
    if meta.get("scenario"):
        lines.append(f"- **Scenario**: `{meta['scenario']}`")
    if meta.get("env"):
        lines.append(f"- **Environment**: `{meta['env']}`")
    if meta.get("sample"):
        lines.append(f"- **Sample**: `{meta['sample']}`")
    return lines


def _ensure_header(
    path: Path,
    *,
    sample_dir: Path,
    load_profile: str | None = None,
    experiment_id: str | None = None,
) -> None:
    if path.is_file() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    experiment = resolve_k8s_experiment_id(experiment_id)
    profile = (load_profile or os.environ.get("BAXBENCH_LOAD_PROFILE", "")).strip() or "default"
    ws_root = k8s_workspace_root(sample_dir, experiment_id=experiment_id)
    header = "\n".join(
        [
            "# K8s experiment summary",
            "",
            f"- **Experiment**: `{experiment}`",
            f"- **Workspace**: [{ws_root.name}](.)",
            *_header_metadata_lines(ws_root),
            f"- **Started**: {_utc_now_label()}",
            f"- **Load profile**: `{profile}`",
            "",
            "Each iteration may include **code**, **spec**, **stage failure**, and "
            "**Locust run** blocks (folder suffix indicates the chosen path). "
            "Load/diagnostics content is inlined in collapsible sections "
            "(not as bare filesystem paths).",
            "",
            "- **LLM cost ledger**: [llm_cost_ledger.json](llm_cost_ledger.json) "
            "(estimated; pass --llm-max-cost to cap spend)",
            "",
            "---",
            "",
        ]
    )
    path.write_text(header, encoding="utf-8")


def _refresh_experiment_summary_header(experiment_root: Path) -> None:
    """Rewrite the header block (up to the first ``---``) with current metadata."""
    path = experiment_summary_path(experiment_root)
    if not path.is_file():
        return
    content = path.read_text(encoding="utf-8")
    m = re.search(r"^---\s*$", content, re.M)
    if not m:
        return
    body = content[m.start():]

    experiment = experiment_root.name
    profile_m = re.search(r"^- \*\*Load profile\*\*: `([^`]*)`", content, re.M)
    profile = profile_m.group(1) if profile_m else "default"
    started_m = re.search(r"^- \*\*Started\*\*: (.+)$", content, re.M)
    started = started_m.group(1) if started_m else _utc_now_label()

    header_lines = [
        "# K8s experiment summary",
        "",
        f"- **Experiment**: `{experiment}`",
        f"- **Workspace**: [{experiment_root.name}](.)",
        *_header_metadata_lines(experiment_root),
        f"- **Started**: {started}",
        f"- **Load profile**: `{profile}`",
        "",
        "Each iteration may include **code**, **spec**, **stage failure**, and "
        "**Locust run** blocks (folder suffix indicates the chosen path). "
        "Load/diagnostics content is inlined in collapsible sections "
        "(not as bare filesystem paths).",
        "",
        "- **LLM cost ledger**: [llm_cost_ledger.json](llm_cost_ledger.json) "
        "(estimated; pass --llm-max-cost to cap spend)",
        "",
    ]
    path.write_text("\n".join(header_lines) + "\n" + body, encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def _iteration_heading_present(path: Path, section_id: str) -> bool:
    if not path.is_file():
        return False
    return re.search(rf"^## {re.escape(section_id)}\s*$", path.read_text(encoding="utf-8"), re.M) is not None


def _maybe_write_iteration_heading(path: Path, section_id: str) -> str:
    """Deprecated for new writes — use :func:`_append_for_iteration` instead."""
    if _iteration_heading_present(path, section_id):
        return ""
    return f"\n## {section_id}\n\n"


def _insert_pos_for_iteration_section(content: str, section_id: str) -> int | None:
    """
    Return the byte offset where new blocks for ``section_id`` should be inserted.

    Inserts immediately before the next ``## iteration-…`` heading, or at EOF if
    this is the last iteration section. Returns ``None`` when the heading is absent.
    """
    heading_re = re.compile(rf"^## {re.escape(section_id)}\s*$", re.M)
    m = heading_re.search(content)
    if not m:
        return None
    after_heading = m.end()
    next_iter = _NEXT_ITERATION_SECTION_RE.search(content[after_heading:])
    if next_iter:
        return after_heading + next_iter.start()
    return len(content)


def _append_for_iteration(path: Path, section_id: str, text: str) -> None:
    """
    Append summary blocks for one iteration, keeping them under that iteration's section.

    On a fresh experiment each iteration heading is written once and blocks are
    appended at EOF (chronological). When an experiment is **continued** and an
    iteration is re-run (e.g. ``iteration-007`` failed first, then succeeded on
    retry), the heading already exists but old blocks must not be orphaned at the
    end of the file — new blocks are inserted at the end of that iteration's
    section (before the next ``## iteration-…`` heading).
    """
    block = text if text.endswith("\n") else text + "\n"

    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"## {section_id}\n\n{block}", encoding="utf-8")
        return

    content = path.read_text(encoding="utf-8")
    if not _iteration_heading_present(path, section_id):
        prefix = "\n" if content and not content.endswith("\n\n") else ""
        _append(path, f"{prefix}## {section_id}\n\n{block}")
        return

    insert_at = _insert_pos_for_iteration_section(content, section_id)
    if insert_at is None:
        _append(path, block)
        return

    before = content[:insert_at].rstrip("\n")
    after = content[insert_at:].lstrip("\n")
    new_content = before + "\n\n" + block.rstrip("\n")
    if after:
        new_content += "\n\n" + after
    new_content += "\n"
    path.write_text(new_content, encoding="utf-8")


def _upsert_iteration_block(
    path: Path,
    section_id: str,
    text: str,
    *,
    block_pattern: re.Pattern[str],
) -> None:
    """Replace the first matching block under an iteration section, or append."""
    block = text if text.endswith("\n") else text + "\n"
    if not path.is_file() or not _iteration_heading_present(path, section_id):
        _append_for_iteration(path, section_id, block)
        return

    content = path.read_text(encoding="utf-8")
    heading_re = re.compile(rf"^## {re.escape(section_id)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        _append_for_iteration(path, section_id, block)
        return

    sec_start = heading.end()
    next_iter = _NEXT_ITERATION_SECTION_RE.search(content[sec_start:])
    sec_end = sec_start + next_iter.start() if next_iter else len(content)
    section = content[sec_start:sec_end]
    block_match = block_pattern.search(section)
    if not block_match:
        _append_for_iteration(path, section_id, block)
        return

    new_section = section[: block_match.start()] + block + section[block_match.end() :]
    path.write_text(content[:sec_start] + new_section + content[sec_end:], encoding="utf-8")


def _iteration_section_span(content: str, section_id: str) -> tuple[int, int, int] | None:
    """
    Return ``(heading_start, body_start, section_end)`` for ``## section_id``.

    ``section_end`` is the start of the next ``## iteration-…`` heading, or EOF.
    """
    heading_re = re.compile(rf"^## {re.escape(section_id)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        return None
    body_start = heading.end()
    next_iter = _NEXT_ITERATION_SECTION_RE.search(content[body_start:])
    section_end = body_start + next_iter.start() if next_iter else len(content)
    return heading.start(), body_start, section_end


def _delete_iteration_section(content: str, section_id: str) -> str:
    """Remove an entire ``## section_id`` section (heading + body)."""
    span = _iteration_section_span(content, section_id)
    if span is None:
        return content
    heading_start, _body_start, section_end = span
    before = content[:heading_start].rstrip("\n")
    after = content[section_end:].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    return before + ("\n" if before else "") + after


def _delete_iteration_block(
    content: str,
    section_id: str,
    block_pattern: re.Pattern[str],
) -> str:
    """Remove the first matching block under one iteration section, if present."""
    span = _iteration_section_span(content, section_id)
    if span is None:
        return content
    _heading_start, sec_start, sec_end = span
    section = content[sec_start:sec_end]
    block_match = block_pattern.search(section)
    if not block_match:
        return content
    new_section = section[: block_match.start()] + section[block_match.end() :]
    return content[:sec_start] + new_section + content[sec_end:]


def _merge_iteration_sections(
    content: str, *, source_id: str, target_id: str
) -> str:
    """
    Move ``source_id`` body into ``target_id`` (prepended), then drop ``source_id``.

    If ``target_id`` is absent, rename the source heading instead.
    """
    if source_id == target_id:
        return content
    source_span = _iteration_section_span(content, source_id)
    if source_span is None:
        return content
    _src_h, src_body_start, src_end = source_span
    source_body = content[src_body_start:src_end].strip("\n")

    target_span = _iteration_section_span(content, target_id)
    if target_span is None:
        content = re.sub(
            rf"^## {re.escape(source_id)}\s*$",
            f"## {target_id}",
            content,
            count=1,
            flags=re.M,
        )
        return content

    # Drop source first so indices into target stay valid only when source is before target.
    content_wo_source = _delete_iteration_section(content, source_id)
    target_span = _iteration_section_span(content_wo_source, target_id)
    if target_span is None or not source_body.strip():
        return content_wo_source
    _tgt_h, tgt_body_start, tgt_end = target_span
    target_body = content_wo_source[tgt_body_start:tgt_end]
    merged_body = source_body + "\n\n" + target_body.lstrip("\n")
    return (
        content_wo_source[:tgt_body_start]
        + "\n\n"
        + merged_body
        + content_wo_source[tgt_end:]
    )


def rename_summary_iteration_section(
    *,
    iteration_path: Path,
    old_section_id: str,
    new_section_id: str,
) -> Path | None:
    """
    After an iteration folder is renamed (e.g. ``iteration-001`` → ``iteration-001-code``),
    rewrite the matching ``experiment_summary.md`` heading so decision + later blocks stay
    under one section.
    """
    if old_section_id == new_section_id:
        return None
    path = experiment_summary_path_for_iteration(iteration_path)
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")
    new_content = _merge_iteration_sections(
        content, source_id=old_section_id, target_id=new_section_id
    )
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")
    return path


def _relative_workspace_link(root: Path, target: Path, *, label: str | None = None) -> str:
    """Markdown link relative to the experiment workspace (clickable in the IDE)."""
    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return f"`{target}`"
    text = label or rel
    return f"[{text}]({rel})"


def _read_ft_counts(iteration_path: Path) -> tuple[int, int] | None:
    from workspace.paths import iteration_functional_tests_dir

    path = iteration_functional_tests_dir(iteration_path) / "test_results.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    passed = int(data.get("num_passed_ft", 0) or 0)
    total = int(data.get("num_total_ft", 0) or 0)
    if total <= 0:
        return None
    return passed, total


def _format_validation_sections(
    *,
    errors: list[str] | tuple[str, ...] = (),
    warnings: list[str] | tuple[str, ...] = (),
) -> str:
    parts: list[str] = []
    if errors:
        parts.extend(["", "### Validation errors", "", *[f"- {e}" for e in errors]])
    if warnings:
        parts.extend(["", "### Validation warnings", "", *[f"- {w}" for w in warnings]])
    return "\n".join(parts)


def _stage_failure_heading(kind: str, *, is_baseline_iter: bool) -> str:
    if kind == "decision":
        return "Decision stage failed"
    if kind == "code":
        return (
            "Baseline code generation failed"
            if is_baseline_iter
            else "Code stage failed"
        )
    if kind == "spec":
        return "Baseline spec stage failed" if is_baseline_iter else "Spec stage failed"
    if kind == "deploy":
        return "Deploy stage failed"
    if kind == "bench":
        return "Bench stage failed"
    return "Iteration failed"


def _spec_diff_source_label(spec_path: Path) -> str:
    """Human label for the iteration whose spec we diff against (not ``03-spec/``)."""
    # spec.yaml lives at ``iteration-NNN-…/03-spec/spec.yaml``
    iteration_dir = spec_path.parent.parent
    if iteration_dir.name.startswith("iteration-"):
        return f"`{iteration_dir.name}`"
    return f"`{spec_path.parent.name}`"


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
    if b.env:
        env_bits = ", ".join(f"{k}={v}" for k, v in sorted(b.env.items()))
        lines.append(f"- **Backend env**: {env_bits}")
    if spec.database.enabled:
        primary = spec.database.effective_primary_resources()
        replica = spec.database.effective_replica_resources()
        lines.append(
            f"- **Database primary** {_format_resources('resources', primary)}"
        )
        if spec.database.replicas > 1:
            lines.append(
                f"- **Database replica** {_format_resources('resources', replica)}"
            )
        elif spec.database.primary_resources is not None:
            lines.append(
                f"- **Database (default)** {_format_resources('resources', spec.database.resources)}"
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
    if spec.pooler.enabled:
        p = spec.pooler
        lines.append(
            f"- **Write pooler**: {p.replicas} replica(s), mode `{p.mode}`, "
            f"pool_size={p.default_pool_size}, max_client_conn={p.max_client_conn}"
        )
    if spec.read_pooler.enabled:
        rp = spec.read_pooler
        lines.append(
            f"- **Read pooler**: {rp.replicas} replica(s), mode `{rp.mode}`, "
            f"pool_size={rp.default_pool_size}, max_client_conn={rp.max_client_conn}"
        )
    if spec.cache.enabled:
        c = spec.cache
        lines.append(
            f"- **Redis cache**: {c.replicas} replica(s), maxmemory={c.maxmemory}, "
            f"policy={c.maxmemory_policy}"
        )
    if spec.backend.placement_workers:
        lines.append(
            f"- **Backend placement workers**: {', '.join(spec.backend.placement_workers)}"
        )
    lines.append(f"- **Backend spread_replicas**: {spec.backend.spread_replicas}")
    return lines


def _previous_spec_path(iteration_path: Path) -> Path | None:
    """
    Locate the most recent prior iteration's ``spec.yaml``.

    Walks back through iteration indices ``[index - 1 .. 0]`` and returns the
    first ``spec.yaml`` found, regardless of whether the iteration folder is
    suffixed ``-failed``. This way a spec block can show the diff against the
    immediately preceding attempt (failed or not), instead of silently saying
    "first iteration" when the previous one happened to crash.
    """
    index, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
    if index is None or index <= 0:
        return None

    parent = iteration_path.parent
    if not parent.is_dir():
        return None

    for target_index in range(index - 1, -1, -1):
        candidates: list[Path] = []
        for child in parent.iterdir():
            if not child.is_dir():
                continue
            p, _k, _f = parse_iteration_folder_name(child.name)
            if p != target_index:
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


def _diff_workers(name: str, prev: tuple[str, ...], cur: tuple[str, ...]) -> str | None:
    if prev == cur:
        return None
    prev_s = ", ".join(prev) if prev else "(any)"
    cur_s = ", ".join(cur) if cur else "(any)"
    return f"- **{name}**: `{prev_s}` → `{cur_s}`"


def _diff_optional_int(name: str, old: int | None, new: int | None) -> str | None:
    if old == new:
        return None
    old_s = str(old) if old is not None else "(unset)"
    new_s = str(new) if new is not None else "(unset)"
    return f"- **{name}**: `{old_s}` → `{new_s}`"


def _diff_env_dict(prev: dict[str, str], cur: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key in sorted(set(prev) | set(cur)):
        old = prev.get(key)
        new = cur.get(key)
        if old == new:
            continue
        old_s = old if old is not None else "(unset)"
        new_s = new if new is not None else "(unset)"
        lines.append(f"- **backend env {key}**: `{old_s}` → `{new_s}`")
    return lines


def _diff_pooler_fields(name: str, prev, cur) -> list[str]:
    lines: list[str] = []
    for line in (
        _diff_field(f"{name} enabled", prev.enabled, cur.enabled),
        _diff_field(f"{name} mode", prev.mode, cur.mode),
        _diff_field(f"{name} replicas", prev.replicas, cur.replicas),
        _diff_field(f"{name} max_client_conn", prev.max_client_conn, cur.max_client_conn),
        _diff_field(
            f"{name} default_pool_size", prev.default_pool_size, cur.default_pool_size
        ),
        _diff_optional_int(f"{name} min_pool_size", prev.min_pool_size, cur.min_pool_size),
        _diff_optional_int(
            f"{name} reserve_pool_size", prev.reserve_pool_size, cur.reserve_pool_size
        ),
        _diff_field(
            f"{name} cpu limit",
            prev.resources.cpu_limit,
            cur.resources.cpu_limit,
        ),
        _diff_field(
            f"{name} cpu request",
            prev.resources.cpu_request,
            cur.resources.cpu_request,
        ),
        _diff_field(
            f"{name} memory limit",
            prev.resources.memory_limit,
            cur.resources.memory_limit,
        ),
        _diff_field(
            f"{name} memory request",
            prev.resources.memory_request,
            cur.resources.memory_request,
        ),
    ):
        if line:
            lines.append(line)
    return lines


def _diff_cache_fields(prev, cur) -> list[str]:
    lines: list[str] = []
    for line in (
        _diff_field("cache enabled", prev.enabled, cur.enabled),
        _diff_field("cache replicas", prev.replicas, cur.replicas),
        _diff_field("cache maxmemory", prev.maxmemory, cur.maxmemory),
        _diff_field("cache maxmemory_policy", prev.maxmemory_policy, cur.maxmemory_policy),
        _diff_field("cache cpu limit", prev.resources.cpu_limit, cur.resources.cpu_limit),
        _diff_field(
            "cache memory limit", prev.resources.memory_limit, cur.resources.memory_limit
        ),
    ):
        if line:
            lines.append(line)
    return lines


def _diff_database_cache_fields(prev, cur) -> list[str]:
    lines: list[str] = []
    for line in (
        _diff_field("database cache enabled", prev.enabled, cur.enabled),
        _diff_field("database cache use_shared", prev.use_shared, cur.use_shared),
    ):
        if line:
            lines.append(line)
    return lines


def _diff_tuning(prev, cur) -> list[str]:
    lines: list[str] = []
    for field in (
        "shared_buffers",
        "effective_cache_size",
        "work_mem",
        "maintenance_work_mem",
        "max_parallel_workers_per_gather",
        "max_parallel_workers",
        "max_worker_processes",
        "random_page_cost",
        "effective_io_concurrency",
        "max_wal_size",
        "checkpoint_timeout_s",
        "wal_buffers",
        "jit_enabled",
        "statement_timeout_ms",
    ):
        old = getattr(prev, field)
        new = getattr(cur, field)
        line = _diff_field(f"database tuning {field}", old or "", new or "")
        if line:
            lines.append(line)
    return lines


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
        _diff_field(
            "backend memory request",
            prev.backend.resources.memory_request,
            cur.backend.resources.memory_request,
        ),
        _diff_field(
            "backend spread_replicas",
            prev.backend.spread_replicas,
            cur.backend.spread_replicas,
        ),
        _diff_workers(
            "backend placement workers",
            prev.backend.placement_workers,
            cur.backend.placement_workers,
        ),
        *_diff_env_dict(prev.backend.env, cur.backend.env),
        _diff_field("database enabled", prev.database.enabled, cur.database.enabled),
        _diff_field(
            "database replicas",
            prev.database.replicas,
            cur.database.replicas,
        ),
        _diff_field(
            "database max_connections",
            prev.database.max_connections,
            cur.database.max_connections,
        ),
        *_diff_tuning(prev.database.tuning, cur.database.tuning),
        _diff_field(
            "database primary cpu limit",
            prev.database.effective_primary_resources().cpu_limit,
            cur.database.effective_primary_resources().cpu_limit,
        ),
        _diff_field(
            "database primary cpu request",
            prev.database.effective_primary_resources().cpu_request,
            cur.database.effective_primary_resources().cpu_request,
        ),
        _diff_field(
            "database primary memory limit",
            prev.database.effective_primary_resources().memory_limit,
            cur.database.effective_primary_resources().memory_limit,
        ),
        _diff_field(
            "database primary memory request",
            prev.database.effective_primary_resources().memory_request,
            cur.database.effective_primary_resources().memory_request,
        ),
        *(
            [
                _diff_field(
                    "database replica cpu limit",
                    prev.database.effective_replica_resources().cpu_limit,
                    cur.database.effective_replica_resources().cpu_limit,
                ),
                _diff_field(
                    "database replica cpu request",
                    prev.database.effective_replica_resources().cpu_request,
                    cur.database.effective_replica_resources().cpu_request,
                ),
                _diff_field(
                    "database replica memory limit",
                    prev.database.effective_replica_resources().memory_limit,
                    cur.database.effective_replica_resources().memory_limit,
                ),
                _diff_field(
                    "database replica memory request",
                    prev.database.effective_replica_resources().memory_request,
                    cur.database.effective_replica_resources().memory_request,
                ),
            ]
            if prev.database.replicas > 1 or cur.database.replicas > 1
            else []
        ),
        _diff_field(
            "database placement worker",
            prev.database.placement_worker or "",
            cur.database.placement_worker or "",
        ),
        _diff_workers(
            "database placement workers",
            prev.database.placement_workers,
            cur.database.placement_workers,
        ),
        *_diff_database_cache_fields(prev.database.cache, cur.database.cache),
        *_diff_pooler_fields("pooler", prev.pooler, cur.pooler),
        *_diff_pooler_fields("read_pooler", prev.read_pooler, cur.read_pooler),
        *_diff_cache_fields(prev.cache, cur.cache),
    ):
        if line:
            changes.append(line)
    if not changes:
        return "No spec changes vs previous iteration."
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
        drift_m = _STEP_DRIFT_RE.search(line)
        samples_m = _SAMPLES_RE.search(line)
        decision_samples: list[float] = []
        if samples_m:
            raw = samples_m.group("samples").strip()
            if raw:
                decision_samples = [float(x.strip()) for x in raw.split(",") if x.strip()]
        phases.append(
            {
                "t_s": t_s,
                "next_users": int(users_m.group(1)) if users_m else None,
                "p95_decision_ms": int(p95_decision.group(1)) if p95_decision else None,
                "p95_logged": _phase_p95_token(m),
                "reqs": int(m.group("reqs")),
                "fail": int(m.group("fail")),
                "fail_pct": _phase_fail_pct(m),
                "action": m.group("action").strip(),
                "step_goodput_rps": float(goodput_m.group(1)) if goodput_m else None,
                "step_cv": float(cv_m.group(1)) if cv_m else None,
                "step_drift_pct": float(drift_m.group(1)) if drift_m else None,
                "decision_samples": decision_samples,
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


def _parse_p95_sample_series(log_text: str) -> list[tuple[int, float]]:
    series: list[tuple[int, float]] = []
    for line in log_text.splitlines():
        m = _P95_SAMPLE_RE.search(line)
        if not m:
            continue
        series.append((int(m.group(1)), float(m.group(3))))
    series.sort(key=lambda item: item[0])
    return series


def _stats_min_avg_max(
    values: list[float],
) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    return min(values), sum(values) / len(values), max(values)


def _format_min_avg_max(
    low: float | None,
    mid: float | None,
    high: float | None,
    *,
    precision: int = 1,
) -> str:
    if low is None or mid is None or high is None:
        return "—"
    fmt = f"{{:.{precision}f}}"
    return f"{fmt.format(low)} / {fmt.format(mid)} / {fmt.format(high)}"


def _load_goodput_timeseries(perf_run_dir: Path | None) -> list[tuple[int, float]] | None:
    if perf_run_dir is None:
        return None
    try:
        from plots.ramp.data import load_stats_timeseries

        df = load_stats_timeseries(perf_run_dir)
    except (FileNotFoundError, ValueError, ImportError):
        return None
    return [
        (int(row["t_s"]), float(row["goodput_rps"]))
        for _, row in df.iterrows()
        if row["goodput_rps"] == row["goodput_rps"]
    ]


def _goodput_window_stats(
    timeseries: list[tuple[int, float]] | None,
    t_end: int,
    *,
    window_s: int = _SUMMARY_MEASURE_WINDOW_S,
) -> tuple[float | None, float | None, float | None]:
    if not timeseries:
        return None, None, None
    t_start = t_end - window_s
    values = [gp for t_s, gp in timeseries if t_start < t_s <= t_end]
    return _stats_min_avg_max(values)


def _p95_window_stats(
    p95_series: list[tuple[int, float]],
    t_end: int,
    decision_samples: list[float],
    *,
    window_s: int = _SUMMARY_MEASURE_WINDOW_S,
) -> tuple[float | None, float | None, float | None]:
    t_start = t_end - window_s
    window_values = [p95 for t_s, p95 in p95_series if t_start < t_s <= t_end]
    if window_values:
        return _stats_min_avg_max(window_values)
    if decision_samples:
        return _stats_min_avg_max(decision_samples)
    return None, None, None


def _adaptive_table_markdown(
    phases: list[dict[str, Any]],
    *,
    initial_users: int | None = None,
    v2_stop: dict[str, Any] | None = None,
    log_text: str = "",
    perf_run_dir: Path | None = None,
) -> str:
    """
    Render the adaptive ramp as a step table.

    - Step 0 = initial level (before any decision); step N = level reached after
      the N-th decision.
    - ``level users`` = virtual users actually running during this measurement
      window (= previous step's ``next users``).
    - ``→ next users`` = users the controller selected for the next window.
    - Goodput min/avg/max = Locust ``stats_history`` over the trailing
      ``_SUMMARY_MEASURE_WINDOW_S`` seconds before each decision (successful req/s).
    - P95 min/avg/max = controller ``adaptive p95 sample`` lines in that same
      trailing window, or the decision ``samples=[…]`` fallback when absent.
    """
    if not phases:
        return "(no `adaptive phase end` lines in bench.log — not an adaptive profile or run too short)"

    p95_series = _parse_p95_sample_series(log_text)
    goodput_timeseries = _load_goodput_timeseries(perf_run_dir)

    header_cols = [
        "Step",
        "window end t (s)",
        "level users",
        "→ next users",
        "goodput (succ/s)<br>min / avg / max",
        "P95 (ms)<br>min / avg / max",
        "fail %",
        "action",
    ]
    header = "| " + " | ".join(header_cols) + " |"
    sep_cells = ["---:"] * (len(header_cols) - 1) + ["---"]
    sep = "|" + "|".join(sep_cells) + "|"
    rows = [header, sep]

    level_users = initial_users
    cumulative_reqs = 0
    cumulative_fail = 0
    step_goodput_maxima: list[float] = []
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
        t_end = int(p["t_s"])
        gp_min, gp_avg, gp_max = _goodput_window_stats(goodput_timeseries, t_end)
        if gp_max is not None:
            step_goodput_maxima.append(gp_max)
        elif p.get("step_goodput_rps") is not None:
            gp_fallback = float(p["step_goodput_rps"])
            gp_min = gp_avg = gp_max = gp_fallback
            step_goodput_maxima.append(gp_fallback)
        p95_min, p95_avg, p95_max = _p95_window_stats(
            p95_series,
            t_end,
            list(p.get("decision_samples") or []),
        )
        cells = [
            str(i),
            str(t_end),
            str(level_users if level_users is not None else "—"),
            str(next_users if next_users is not None else "—"),
            _format_min_avg_max(gp_min, gp_avg, gp_max, precision=0),
            _format_min_avg_max(p95_min, p95_avg, p95_max, precision=0),
            step_fail_pct,
            p["action"].replace("|", "\\|")[:80],
        ]
        rows.append("| " + " | ".join(cells) + " |")
        level_users = next_users

    last = phases[-1]
    rows.append("")
    rows.append(
        f"_Goodput min/avg/max uses Locust ``stats_history`` over the last "
        f"{_SUMMARY_MEASURE_WINDOW_S}s before each decision; P95 min/avg/max uses "
        f"controller ``adaptive p95 sample`` lines in that window (or decision "
        f"``samples=[…]`` when per-second samples are unavailable)._"
    )
    if step_goodput_maxima:
        rows.append(
            f"**Table peak goodput**: **{max(step_goodput_maxima):.1f}** succ/s "
            f"(max of goodput-max column)."
        )
    try:
        from plots.ramp.data import (
            gather_bench_log_text,
            is_explore_refine_bench,
            peak_goodput_from_bench_log,
            sustained_goodput_from_bench,
            sustained_goodput_skip_reason,
        )

        if perf_run_dir is not None:
            perf_log = gather_bench_log_text(perf_run_dir)
            sustained = sustained_goodput_from_bench(perf_run_dir, log_text=perf_log)
            if sustained is not None and sustained.goodput_rps > 0:
                phase_note = (
                    " (refine phase only)"
                    if is_explore_refine_bench(perf_run_dir)
                    else ""
                )
                rows.append(
                    f"**Sustained max goodput ({sustained.window_s}s window)**: "
                    f"**{sustained.goodput_rps:.1f}** succ/s @ {sustained.users}u "
                    f"(t={sustained.t_s}s, fail={sustained.fail_pct:.1f}%, "
                    f"drift={sustained.drift_pct:.1f}%){phase_note} — primary experiment metric."
                )
            elif is_explore_refine_bench(perf_run_dir):
                from plots.ramp.data import classify_bench_run_outcome

                outcome = classify_bench_run_outcome(perf_run_dir, perf_log)
                if outcome is not None:
                    rows.append(f"**Run outcome**: {outcome.title} — {outcome.summary}")
                else:
                    skip = sustained_goodput_skip_reason(perf_run_dir, perf_log)
                    rows.append(
                        f"**Sustained max goodput**: not recorded "
                        f"({skip or 'refine phase not scored'})."
                    )
        run_peak, peak_users = peak_goodput_from_bench_log(log_text)
        if run_peak > 0 and (
            perf_run_dir is None or not is_explore_refine_bench(perf_run_dir)
        ):
            user_note = f" @ {peak_users}u" if peak_users is not None else ""
            rows.append(
                f"**Step peak goodput (controller metric)**: **{run_peak:.1f}** "
                f"succ/s{user_note} — legacy per-step maximum."
            )
    except ImportError:
        pass
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
    for stats_path in sorted((perf_run_dir / "locust" / "results").glob("*_stats.csv")):
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
    return "(no locust/results/*_stats.csv found)"


def _perf_run_load_profile_line(perf_run_dir: Path) -> str:
    from bench_diagnostics.summary import load_profile_from_config, read_run_config

    cfg = read_run_config(perf_run_dir)
    name = load_profile_from_config(cfg) or "default"
    resolved = cfg.get("resolved_load_profile")
    if isinstance(resolved, dict):
        mode = resolved.get("mode") or resolved.get("profile_mode") or ""
        run_time = resolved.get("run_time_s")
        extras: list[str] = []
        if mode:
            extras.append(str(mode))
        if run_time is not None:
            extras.append(f"run_time={run_time}s")
        if extras:
            return f"- **Load profile**: `{name}` ({', '.join(extras)})"
    return f"- **Load profile**: `{name}`"


def _adaptive_ramp_plot_markdown(
    experiment_root: Path,
    perf_run_dir: Path,
) -> str:
    from plots.ramp.plot import ADAPTIVE_RAMP_PLOT_FILENAME

    plot_path = perf_run_dir / PLOTS_DIRNAME / ADAPTIVE_RAMP_PLOT_FILENAME
    if not plot_path.is_file():
        return ""
    try:
        rel = plot_path.resolve().relative_to(experiment_root.resolve()).as_posix()
    except ValueError:
        return ""
    return f"![Adaptive load ramp]({rel})"


def _load_codegen_meta(iteration_path: Path) -> dict[str, Any]:
    from workspace import baseline_codegen_meta_path

    meta_path = baseline_codegen_meta_path(iteration_path)
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _build_baseline_codegen_block_text(
    *,
    iteration_path: Path,
    experiment_root: Path,
    status: str,
    attempts_used: int,
    max_attempts: int,
    winning_attempt: int | None,
    error: str | None,
) -> str:
    """Body for the baseline ``### Baseline code generation`` subsection.

    Layout: overall status / attempts used / code link first, then a per-attempt
    section listing each try and the failure that occurred.
    """
    from workspace import iteration_code_attempts_dir as _attempts_dir

    meta = _load_codegen_meta(iteration_path)
    attempts_data = [
        a for a in (meta.get("attempts") or []) if isinstance(a, dict)
    ]

    attempt_blocks: list[str] = []
    any_infra = False
    for a in attempts_data:
        idx = a.get("attempt_index", "?")
        st = a.get("status", "?")
        ft_pass = a.get("num_passed_ft")
        ft_total = a.get("num_total_ft")
        err = (a.get("error") or "").strip()
        is_infra = bool(a.get("infra_failure"))
        if is_infra:
            any_infra = True
        ft_part = (
            f"FT={ft_pass}/{ft_total}"
            if ft_pass is not None and ft_total is not None
            else "FT=—"
        )
        infra_tag = " **[infra]**" if is_infra else ""
        attempt_link = _relative_workspace_link(
            experiment_root,
            _attempts_dir(iteration_path) / f"{int(idx):03d}",
            label=f"attempt {idx}",
        )
        line = f"- **Attempt {idx}** ({attempt_link}): `{st}`{infra_tag} ({ft_part})"
        if err:
            err_excerpt = err if len(err) <= 200 else err[:200].rstrip() + "…"
            line += f" — {err_excerpt}"
        attempt_blocks.append(line)

        if is_infra:
            log_excerpt = (a.get("error_excerpt") or "").strip()
            if log_excerpt:
                tail = log_excerpt[-800:]
                attempt_blocks.append("")
                attempt_blocks.append("  <details><summary>test.log tail</summary>")
                attempt_blocks.append("")
                attempt_blocks.append("  ```")
                for ln in tail.splitlines():
                    attempt_blocks.append(f"  {ln}")
                attempt_blocks.append("  ```")
                attempt_blocks.append("")
                attempt_blocks.append("  </details>")

    code_link = _relative_workspace_link(
        experiment_root, iteration_path / "02-code" / "code", label="02-code/code"
    )
    body = "\n".join(
        [
            f"### Baseline code generation ({_utc_now_label()})",
            "",
            f"- **Status**: `{status}`"
            + (f" (winning attempt: **{winning_attempt}**)" if winning_attempt else ""),
            f"- **Attempts used**: {attempts_used} / {max_attempts}",
            f"- **Code**: {code_link}",
            "",
            "**Attempts**" if attempt_blocks else "**Attempts**: (none recorded)",
            "",
            *attempt_blocks,
            "",
            *(
                [
                    "**Failure reason**"
                    + (
                        " — host environment issue (no LLM retries spent)"
                        if status == "infra_failed" or any_infra
                        else ""
                    ),
                    "",
                    f"> {error}" if error else "> (no error message recorded)",
                    "",
                ]
                if status != "passed"
                else []
            ),
            "---",
            "",
        ]
    )
    return body


def append_baseline_codegen_block(
    *,
    sample_dir: Path,
    iteration_path: Path,
    task: Any,
    attempts_used: int,
    max_attempts: int,
    winning_attempt: int | None,
    status: str,
    error: str | None,
    load_profile: str | None = None,
) -> Path:
    """
    Append a baseline-codegen subsection under the baseline iteration.

    Renders one line per attempt (status + FT pass counts + error excerpt) so
    a reader can see at a glance how many tries it took to get the application
    code to pass the functional test suite — and which attempts' transcripts
    live under ``02-code/attempts/<NNN>/`` for forensics on the failures.
    """
    del task
    path = experiment_summary_path_for_iteration(iteration_path)
    experiment_root = experiment_root_from_iteration_path(iteration_path)
    _ensure_header(
        path,
        sample_dir=experiment_root.parent.parent,
        load_profile=load_profile,
        experiment_id=experiment_root.name,
    )
    iid = _summary_section_id(iteration_path)
    body = _build_baseline_codegen_block_text(
        iteration_path=iteration_path,
        experiment_root=experiment_root,
        status=status,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        winning_attempt=winning_attempt,
        error=error,
    )
    _upsert_iteration_block(
        path, iid, body, block_pattern=_BASELINE_CODEGEN_BLOCK_RE
    )
    return path


def _spec_attempts_section(iteration_path: Path, experiment_root: Path) -> str:
    """Attempt breakdown for a spec-generation block (baseline design + validation).

    Mirrors the baseline code layout: reports how many attempts were spent and,
    for each failed attempt under ``03-spec/attempts/<NNN>/``, the validation
    failure that occurred. Returns ``""`` when there is nothing useful to show.
    """
    from workspace import iteration_spec_attempts_dir

    attempts_dir = iteration_spec_attempts_dir(iteration_path)
    failed: list[tuple[int, dict[str, Any], Path]] = []
    if attempts_dir.is_dir():
        for child in sorted(attempts_dir.iterdir()):
            if not child.is_dir():
                continue
            try:
                idx = int(child.name)
            except ValueError:
                continue
            meta_path = child / "attempt.json"
            data: dict[str, Any] = {}
            if meta_path.is_file():
                try:
                    loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, json.JSONDecodeError):
                    data = {}
            failed.append((idx, data, child))

    # The winning attempt is the top-level spec (not rotated into attempts/).
    winning_index = len(failed) + 1
    attempts_used = winning_index

    lines = [
        "",
        f"- **Attempts used**: {attempts_used} (design + validation; winning attempt: **{winning_index}**)",
        "",
        "**Attempts**",
        "",
    ]
    for idx, data, child in failed:
        link = _relative_workspace_link(
            experiment_root, child, label=f"attempt {idx}"
        )
        status = data.get("status", "failed")
        detail = (data.get("error") or data.get("validation_feedback") or "").strip()
        line = f"- **Attempt {idx}** ({link}): `{status}`"
        if detail:
            excerpt = detail if len(detail) <= 200 else detail[:200].rstrip() + "…"
            line += f" — {excerpt}"
        lines.append(line)
    lines.append(
        f"- **Attempt {winning_index}**: `passed` (spec validated against cluster capacity)"
    )
    return "\n".join(lines)


def _build_spec_generation_block_text(
    *,
    iteration_path: Path,
    spec: K8sWorkloadSpec,
    warnings: list[str],
    errors: list[str] | None = None,
    had_prior_feedback: bool,
    iteration_index: int,
) -> str:
    experiment_root = experiment_root_from_iteration_path(iteration_path)
    prev_path = _previous_spec_path(iteration_path)
    if prev_path:
        try:
            diff_text = _spec_diff_markdown(
                K8sWorkloadSpec.from_yaml_file(prev_path), spec
            )
            diff_source = _spec_diff_source_label(prev_path)
        except ValueError:
            diff_text = f"(could not load previous spec at `{prev_path}`)"
            diff_source = _spec_diff_source_label(prev_path)
    else:
        diff_text = "First iteration in this experiment (no prior spec to diff)."
        diff_source = "—"

    validation_block = _format_validation_sections(
        errors=errors or (),
        warnings=warnings,
    )
    spec_link = _relative_workspace_link(
        experiment_root,
        iteration_spec_path(iteration_path),
        label="spec.yaml",
    )

    lines = [
        f"### Spec generation ({_utc_now_label()})",
        "",
        f"- **Iteration index**: {iteration_index}",
        f"- **Prior Locust feedback in prompt**: {'yes' if had_prior_feedback else 'no (first iteration)'}",
        f"- **Spec**: {spec_link}",
    ]
    attempts_section = (
        _spec_attempts_section(iteration_path, experiment_root)
        if iteration_index == 0 or (iteration_path / "03-spec" / "attempts").is_dir()
        else ""
    )
    if attempts_section:
        lines.append(attempts_section)
    lines.extend(
        [
            "",
            "**Deployment**",
            "",
            "\n".join(_spec_bullets(spec)),
            "",
            f"**Changes vs {diff_source}**",
            "",
            diff_text,
        ]
    )
    if validation_block:
        lines.append(validation_block)
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def append_spec_generation_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    spec: K8sWorkloadSpec,
    warnings: list[str],
    errors: list[str] | None = None,
    had_prior_feedback: bool,
    iteration_index: int,
    load_profile: str | None = None,
) -> Path:
    """Append spec-generation subsection for one iteration."""
    if not _should_record_spec_generation(iteration_path):
        return experiment_summary_path_for_iteration(iteration_path)
    path = experiment_summary_path_for_iteration(iteration_path)
    experiment_root = experiment_root_from_iteration_path(iteration_path)
    _ensure_header(
        path,
        sample_dir=experiment_root.parent.parent,
        load_profile=load_profile,
        experiment_id=experiment_root.name,
    )
    iid = _summary_section_id(iteration_path)

    body = _build_spec_generation_block_text(
        iteration_path=iteration_path,
        spec=spec,
        warnings=warnings,
        errors=errors,
        had_prior_feedback=had_prior_feedback,
        iteration_index=iteration_index,
    )
    _upsert_iteration_block(
        path, iid, body, block_pattern=_SPEC_GENERATION_BLOCK_RE
    )
    return path


def _build_perf_run_block_text(
    *,
    perf_run_dir: Path,
    feedback: IterationFeedback | None = None,
    experiment_root: Path | None = None,
) -> str:
    """Markdown body for one iteration's ``### Locust run`` subsection."""
    log_text = _gather_perf_log_text(perf_run_dir)

    t0, t1 = _parse_bench_log_times(log_text)
    if t0 and t1:
        time_range = f"{t0} – {t1}"
    else:
        time_range = perf_run_dir.name

    locust_line = _aggregate_locust_line(perf_run_dir)
    load_profile_line = _perf_run_load_profile_line(perf_run_dir)

    if experiment_root is None:
        experiment_root = experiment_root_from_iteration_path(perf_run_dir.parent)
    ramp_plot = _adaptive_ramp_plot_markdown(experiment_root, perf_run_dir)

    fb = feedback
    if fb is None:
        from workspace import load_feedback

        fb = load_feedback(perf_run_dir)

    if fb is not None:
        load_section = _collapsible_details(
            "Load test results", fb.load_test_summary_text()
        )
        diag_section = _collapsible_details(
            "Diagnostics", fb.diagnostics_prompt_text()
        )
    else:
        phases = _parse_adaptive_phases(log_text)
        initial_users = _parse_initial_users(log_text)
        v2_stop = _parse_adaptive_v2_stop(log_text)
        adaptive_md = _adaptive_table_markdown(
            phases,
            initial_users=initial_users,
            v2_stop=v2_stop,
            log_text=log_text,
            perf_run_dir=perf_run_dir,
        )
        load_body = "\n".join(
            [
                "### Locust (per endpoint)",
                "",
                "(no iteration_feedback.json — endpoint table unavailable)",
                "",
                "### Locust HTTP errors",
                "",
                "(no iteration_feedback.json)",
            ]
        )
        if not ramp_plot:
            load_body = "\n".join(
                [
                    "**Adaptive ramp** (from `bench.log` + `logs/**/locust-*.log`)",
                    "",
                    adaptive_md,
                    "",
                    load_body,
                ]
            )
        load_section = _collapsible_details(
            "Load test results (legacy fallback — no iteration_feedback.json)",
            load_body,
        )
        diag_section = _format_diagnostics_metrics_for_summary(perf_run_dir) or ""

    notes_line = ""
    if fb and fb.notes and fb.notes.strip():
        notes_line = f"\n**Notes**\n\n{fb.notes.strip()}\n"

    ramp_block = ""
    if ramp_plot:
        ramp_block = f"\n{ramp_plot}\n"

    return "\n".join(
        [
            f"### Locust run ({time_range})",
            "",
            f"- **Recorded**: {_utc_now_label()}",
            load_profile_line,
            "",
            locust_line,
            ramp_block,
            load_section,
            diag_section,
            notes_line,
            "",
            "---",
            "",
        ]
    )


def _replace_locust_run_block(
    content: str,
    section_id: str,
    new_block: str,
) -> tuple[str, bool]:
    """Replace the ``### Locust run`` subsection under one iteration heading."""
    heading_re = re.compile(rf"^## {re.escape(section_id)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        return content, False

    sec_start = heading.end()
    next_iter = _NEXT_ITERATION_SECTION_RE.search(content[sec_start:])
    sec_end = sec_start + next_iter.start() if next_iter else len(content)
    section = content[sec_start:sec_end]

    block_re = re.compile(r"^### Locust run \([^)]*\)\n.*?\n---\n", re.M | re.S)
    block = block_re.search(section)
    if not block:
        return content, False

    new_section = section[: block.start()] + new_block + section[block.end() :]
    new_content = content[:sec_start] + new_section + content[sec_end:]
    return new_content, True


def _replace_spec_generation_block(
    content: str,
    section_id: str,
    new_block: str,
) -> tuple[str, bool]:
    """Replace the ``### Spec generation`` subsection under one iteration heading."""
    heading_re = re.compile(rf"^## {re.escape(section_id)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        return content, False

    sec_start = heading.end()
    next_iter = _NEXT_ITERATION_SECTION_RE.search(content[sec_start:])
    sec_end = sec_start + next_iter.start() if next_iter else len(content)
    section = content[sec_start:sec_end]

    block_re = re.compile(r"^### Spec generation \([^)]*\)\n.*?\n---\n", re.M | re.S)
    block = block_re.search(section)
    if not block:
        return content, False

    new_section = section[: block.start()] + new_block + section[block.end() :]
    new_content = content[:sec_start] + new_section + content[sec_end:]
    return new_content, True


def _iteration_folder_map(
    experiment_root: Path, *, include_failed: bool = False
) -> dict[int, str]:
    """Map iteration index → on-disk folder name (prefer non-failed when both exist)."""
    from workspace import iteration_folder_is_failed

    success: dict[int, str] = {}
    failed: dict[int, str] = {}
    iterations_dir = experiment_root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return success
    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir():
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        if iteration_folder_is_failed(child.name):
            failed[idx] = child.name
        else:
            success[idx] = child.name
    if not include_failed:
        return success
    mapping = dict(failed)
    mapping.update(success)
    return mapping


def migrate_summary_section_headings(content: str, experiment_root: Path) -> str:
    """
    Fold legacy bare ``## iteration-NNN`` sections into on-disk folder slugs.

    Decision is written before the folder gets a ``-code``/``-spec`` suffix; without
    this merge the summary keeps an orphan decision section next to the real one.
    """
    for idx, folder_name in sorted(
        _iteration_folder_map(experiment_root, include_failed=True).items()
    ):
        bare = f"iteration-{idx:03d}"
        if bare == folder_name:
            continue
        content = _merge_iteration_sections(
            content, source_id=bare, target_id=folder_name
        )
    return content


def strip_irrelevant_summary_blocks(content: str, experiment_root: Path) -> str:
    """Drop reused/spec/decision blocks that do not belong under an iteration heading."""
    content = _CODE_REUSE_BLOCK_RE.sub("", content)
    iterations_dir = experiment_root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return content
    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir():
            continue
        section_id = child.name
        kind = _iteration_folder_kind(child)
        # Once code/spec/baseline is chosen, the path is clear from the section
        # name + stage block; the prior "refinement decision" narrative is noise.
        if kind in {"code", "spec", "baseline"}:
            while True:
                updated = _delete_iteration_block(
                    content, section_id, _REFINEMENT_DECISION_BLOCK_RE
                )
                if updated == content:
                    break
                content = updated
        if kind == "code":
            content = _delete_iteration_block(
                content, section_id, _SPEC_GENERATION_BLOCK_RE
            )
    # Drop leftover bare ``## iteration-NNN`` sections (no on-disk bare folder).
    for match in list(re.finditer(r"^## (iteration-\d{3})\s*$", content, re.M)):
        bare_id = match.group(1)
        if (iterations_dir / bare_id).is_dir():
            continue
        content = _delete_iteration_section(content, bare_id)
    return content


def regenerate_experiment_summary_spec_blocks(experiment_root: Path) -> Path:
    """Rebuild every ``### Spec generation`` subsection from on-disk specs."""
    from workspace import (
        ITERATIONS_DIRNAME,
        iteration_folder_is_failed,
        parse_iteration_index,
    )

    experiment_path = experiment_root.expanduser().resolve()
    if (experiment_path / ITERATIONS_DIRNAME).is_dir():
        root = experiment_path
    else:
        from workspace import k8s_workspace_root

        root = k8s_workspace_root(experiment_path)
    path = experiment_summary_path(root)
    if not path.is_file():
        return path

    content = path.read_text(encoding="utf-8")
    iterations_dir = root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return path

    content = migrate_summary_section_headings(content, root)
    content = strip_irrelevant_summary_blocks(content, root)

    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        section_id = child.name
        if not _should_record_spec_generation(child):
            content = _delete_iteration_block(content, section_id, _SPEC_GENERATION_BLOCK_RE)
            continue
        spec_path = find_iteration_spec_path(child)
        if spec_path is None:
            continue
        try:
            spec = K8sWorkloadSpec.from_yaml_file(spec_path)
        except ValueError:
            continue

        block = _build_spec_generation_block_text(
            iteration_path=child,
            spec=spec,
            warnings=[],
            errors=[],
            had_prior_feedback=idx > 0,
            iteration_index=idx,
        )
        updated_content, replaced = _replace_spec_generation_block(content, section_id, block)
        if replaced:
            content = updated_content
        else:
            path.write_text(content, encoding="utf-8")
            _append_for_iteration(path, section_id, block)
            content = path.read_text(encoding="utf-8")

    path.write_text(content, encoding="utf-8")
    return path


def regenerate_experiment_summary_perf_blocks(
    experiment_root: Path,
    *,
    load_profile: str | None = None,
) -> Path:
    """
    Rebuild every ``### Locust run`` subsection from on-disk ``05-bench/`` logs.

    Useful after changing adaptive table formatting without re-running Locust.
    """
    from workspace import (
        ITERATIONS_DIRNAME,
        iteration_bench_dir,
        iteration_folder_is_failed,
        load_feedback,
        parse_iteration_index,
    )

    experiment_path = experiment_root.expanduser().resolve()
    if (experiment_path / ITERATIONS_DIRNAME).is_dir():
        root = experiment_path
    else:
        from workspace import k8s_workspace_root

        root = k8s_workspace_root(experiment_path)
    path = experiment_summary_path(root)
    if not path.is_file():
        _ensure_header(path, sample_dir=root, load_profile=load_profile)

    content = path.read_text(encoding="utf-8")
    iterations_dir = root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return path

    content = migrate_summary_section_headings(content, root)

    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        if parse_iteration_index(child.name) is None:
            continue
        bench_dir = iteration_bench_dir(child)
        if not (bench_dir / "bench.log").is_file():
            continue

        section_id = child.name
        block = _build_perf_run_block_text(
            perf_run_dir=bench_dir,
            feedback=load_feedback(bench_dir),
            experiment_root=root,
        )
        updated_content, replaced = _replace_locust_run_block(content, section_id, block)
        if replaced:
            content = updated_content
        else:
            path.write_text(content, encoding="utf-8")
            _append_for_iteration(path, section_id, block)
            content = path.read_text(encoding="utf-8")

    path.write_text(content, encoding="utf-8")
    return path


def regenerate_experiment_summary_baseline_blocks(experiment_root: Path) -> Path:
    """Rebuild every ``### Baseline code generation`` block from ``codegen.json``."""
    from workspace import (
        ITERATIONS_DIRNAME,
        iteration_folder_is_failed,
        k8s_workspace_root,
        parse_iteration_index,
    )

    experiment_path = experiment_root.expanduser().resolve()
    if (experiment_path / ITERATIONS_DIRNAME).is_dir():
        root = experiment_path
    else:
        root = k8s_workspace_root(experiment_path)
    path = experiment_summary_path(root)
    if not path.is_file():
        return path

    content = path.read_text(encoding="utf-8")
    iterations_dir = root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return path

    content = migrate_summary_section_headings(content, root)

    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        if parse_iteration_index(child.name) is None:
            continue
        meta = _load_codegen_meta(child)
        if not meta:
            continue
        section_id = child.name
        block = _build_baseline_codegen_block_text(
            iteration_path=child,
            experiment_root=root,
            status=str(meta.get("status", "?")),
            attempts_used=int(meta.get("attempts_used", 0) or 0),
            max_attempts=int(meta.get("max_attempts", 0) or 0),
            winning_attempt=meta.get("winning_attempt"),
            error=meta.get("error"),
        )
        updated_content, replaced = _replace_baseline_codegen_block(content, section_id, block)
        if replaced:
            content = updated_content
        else:
            path.write_text(content, encoding="utf-8")
            _append_for_iteration(path, section_id, block)
            content = path.read_text(encoding="utf-8")

    path.write_text(content, encoding="utf-8")
    return path


def _replace_baseline_codegen_block(
    content: str, section_id: str, new_block: str
) -> tuple[str, bool]:
    """Replace the ``### Baseline code generation`` subsection under one iteration."""
    heading_re = re.compile(rf"^## {re.escape(section_id)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        return content, False
    sec_start = heading.end()
    next_iter = _NEXT_ITERATION_SECTION_RE.search(content[sec_start:])
    sec_end = sec_start + next_iter.start() if next_iter else len(content)
    section = content[sec_start:sec_end]
    block = _BASELINE_CODEGEN_BLOCK_RE.search(section)
    if not block:
        return content, False
    new_section = section[: block.start()] + new_block + section[block.end() :]
    return content[:sec_start] + new_section + content[sec_end:], True


def regenerate_experiment_summary(
    experiment_root: Path,
    *,
    load_profile: str | None = None,
) -> Path:
    """Migrate headings, strip irrelevant blocks, and rebuild all iteration blocks."""
    from workspace import k8s_workspace_root

    experiment_path = experiment_root.expanduser().resolve()
    if (experiment_path / ITERATIONS_DIRNAME).is_dir():
        root = experiment_path
    else:
        root = k8s_workspace_root(experiment_path)
    path = experiment_summary_path(root)
    if path.is_file():
        content = migrate_summary_section_headings(path.read_text(encoding="utf-8"), root)
        content = strip_irrelevant_summary_blocks(content, root)
        path.write_text(content, encoding="utf-8")
    _refresh_experiment_summary_header(root)
    regenerate_experiment_summary_baseline_blocks(root)
    regenerate_experiment_summary_spec_blocks(root)
    regenerate_experiment_summary_perf_blocks(root, load_profile=load_profile)
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
    del sample_dir
    iteration_path = perf_run_dir.parent
    path = experiment_summary_path_for_iteration(iteration_path)
    experiment_root = experiment_root_from_iteration_path(iteration_path)
    _ensure_header(
        path,
        sample_dir=experiment_root.parent.parent,
        load_profile=load_profile,
        experiment_id=experiment_root.name,
    )
    iid = _summary_section_id(iteration_path)

    body = _build_perf_run_block_text(perf_run_dir=perf_run_dir, feedback=feedback)
    _upsert_iteration_block(path, iid, body, block_pattern=_LOCUST_RUN_BLOCK_RE)
    return path


def _collapsible_details(summary: str, body: str) -> str:
    return "\n".join(
        [
            "",
            "<details>",
            f"<summary><strong>{summary}</strong></summary>",
            "",
            body.strip(),
            "",
            "</details>",
            "",
        ]
    )


def _max_connections_from_perf_run(perf_run_dir: Path) -> int | None:
    cfg_path = perf_run_dir / "config.json"
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return int(
            (cfg.get("k8s_workload_spec") or {}).get("database", {}).get("max_connections")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _format_diagnostics_metrics_for_summary(perf_run_dir: Path) -> str:
    """Full diagnostics collapsible when ``iteration_feedback.json`` is unavailable."""
    diag_root = perf_run_dir / "diagnostics" / "kubernetes"
    if not diag_root.is_dir():
        return ""

    try:
        from bench_diagnostics.summary import summarize_run_dir

        bench_log_path = perf_run_dir / "bench.log"
        bench_log = ""
        if bench_log_path.is_file():
            bench_log = bench_log_path.read_text(encoding="utf-8", errors="replace")
        diag = summarize_run_dir(
            perf_run_dir,
            bench_log=bench_log,
            max_connections=_max_connections_from_perf_run(perf_run_dir),
        )
    except Exception:
        return ""

    body = "\n".join(
        [
            "Pod logs, then run-scoped metrics (PostgreSQL, replication, pooler, "
            "cache, pod health, cluster events, ``kubectl top``). "
            "Bursty metrics use **min / p50 / avg / p95 / max** over samples.",
            "",
            diag.to_prompt_block().strip() or "(no diagnostics collected)",
        ]
    )
    return _collapsible_details("Diagnostics", body)


def append_iteration_failure_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    failure_reason: str,
    kind: str,
    error_excerpt: str = "",
    load_profile: str | None = None,
    iteration_failure=None,
) -> Path:
    """
    Append a phase-specific failure block for an iteration that never completed bench.

    When a structured failure record is available, renders ``to_prompt_block()`` so
    the summary matches what downstream LLM prompts see.
    """
    del sample_dir
    path = experiment_summary_path_for_iteration(iteration_path)
    experiment_root = experiment_root_from_iteration_path(iteration_path)
    _ensure_header(
        path,
        sample_dir=experiment_root.parent.parent,
        load_profile=load_profile,
        experiment_id=experiment_root.name,
    )
    iid = _summary_section_id(iteration_path)

    from .failure import load_terminal_failure_record

    record: object | None = None
    if iteration_failure is not None:
        record = iteration_failure.terminal
    else:
        try:
            record = load_terminal_failure_record(iteration_path, phase=kind)  # type: ignore[arg-type]
        except Exception:
            record = None

    is_baseline_iter = "-baseline" in iteration_path.name
    heading = _stage_failure_heading(kind, is_baseline_iter=is_baseline_iter)
    body_lines = [
        f"### {heading} ({_utc_now_label()})",
        "",
        f"- **Folder**: `{iteration_path.name}`",
    ]

    if record is not None and hasattr(record, "to_prompt_block"):
        body_lines.extend(["", record.to_prompt_block()])
    else:
        body_lines.extend(
            [
                "",
                f"- **Reason**: {failure_reason or '(no reason recorded)'}",
            ]
        )
        excerpt = (error_excerpt or "").strip()
        if excerpt:
            if len(excerpt) > _MAX_NARRATIVE_CHARS:
                excerpt = excerpt[:_MAX_NARRATIVE_CHARS].rstrip() + "\n…(truncated)"
            body_lines.extend(["", "**Error excerpt**", "", "```", excerpt, "```"])

    body_lines.extend(["", "---", ""])
    block_pattern = (
        _BASELINE_CODE_FAILED_BLOCK_RE
        if kind == "code" and is_baseline_iter
        else _STAGE_FAILURE_BLOCK_RE
    )
    _upsert_iteration_block(
        path, iid, "\n".join(body_lines), block_pattern=block_pattern
    )
    return path


def append_code_refinement_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    load_profile: str | None = None,
) -> Path:
    """Record a successful code-refinement iteration (functional tests passed)."""
    del sample_dir
    path = experiment_summary_path_for_iteration(iteration_path)
    experiment_root = experiment_root_from_iteration_path(iteration_path)
    _ensure_header(
        path,
        sample_dir=experiment_root.parent.parent,
        load_profile=load_profile,
        experiment_id=experiment_root.name,
    )
    iid = _summary_section_id(iteration_path)
    ft_counts = _read_ft_counts(iteration_path)
    ft_line = (
        f"- **Functional tests**: {ft_counts[0]}/{ft_counts[1]} passed"
        if ft_counts is not None
        else "- **Functional tests**: (counts unavailable)"
    )
    body = "\n".join(
        [
            f"### Code refinement ({_utc_now_label()})",
            "",
            "- **Status**: `passed`",
            ft_line,
            "",
            "---",
            "",
        ]
    )
    _upsert_iteration_block(
        path, iid, body, block_pattern=_CODE_REFINEMENT_BLOCK_RE
    )
    return path


def append_refinement_decision_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    decision: Any,
    load_profile: str | None = None,
) -> Path:
    """
    No-op for the markdown summary.

    The code-vs-spec choice is already visible from the iteration folder suffix
    (``iteration-NNN-code`` / ``-spec``) and the corresponding stage block.
    Full rationale remains on disk under ``01-decision/decision.json``.
    """
    del sample_dir, iteration_id, decision, load_profile
    return experiment_summary_path_for_iteration(iteration_path)

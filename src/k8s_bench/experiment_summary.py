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

from .feedback import IterationFeedback
from .workspace import (
    ITERATIONS_DIRNAME,
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
_STEP_DRIFT_RE = re.compile(r"\bdrift=([\d.]+)%")
_SAMPLES_RE = re.compile(r"samples=\[(?P<samples>[^\]]*)\]")
_P95_SAMPLE_RE = re.compile(
    r"adaptive p95 sample t=(\d+)s users=(\d+) p95=([\d.]+)ms"
)
# Trailing Locust stats window for per-step goodput min/avg/max in the summary table.
_SUMMARY_MEASURE_WINDOW_S = 3
_ADAPTIVE_V2_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) "
    r"final_users=(?P<final_users>\S+) "
    r"low_ok=(?P<low_ok>\S+) "
    r"high_bad=(?P<high_bad>\S+) "
    r"goodput_history=\[(?P<history>[^\]]*)\]"
)


def experiment_summary_path(sample_dir: Path) -> Path:
    path = sample_dir.expanduser().resolve()
    if (path / ITERATIONS_DIRNAME).is_dir():
        return path / SUMMARY_FILENAME
    return k8s_workspace_root(path) / SUMMARY_FILENAME


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
            "Each iteration below has a **spec generation** block (deployment snapshot, "
            "full **Changes vs** prior iteration, rationale) and a **Locust run** block "
            "(adaptive ramp; collapsible utilization + run metrics when collected).",
            "",
            f"- **LLM cost ledger**: `{k8s_workspace_root(sample_dir) / 'llm_cost_ledger.json'}` "
            "(estimated; pass --llm-max-cost to cap spend)",
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
    """Deprecated for new writes — use :func:`_append_for_iteration` instead."""
    iid = normalize_iteration_id(iteration_id)
    if _iteration_heading_present(path, iid):
        return ""
    return f"\n## {iid}\n\n"


def _insert_pos_for_iteration_section(content: str, iteration_id: str) -> int | None:
    """
    Return the byte offset where new blocks for ``iteration_id`` should be inserted.

    Inserts immediately before the next ``## iteration-…`` heading, or at EOF if
    this is the last iteration section. Returns ``None`` when the heading is absent.
    """
    iid = normalize_iteration_id(iteration_id)
    heading_re = re.compile(rf"^## {re.escape(iid)}\s*$", re.M)
    m = heading_re.search(content)
    if not m:
        return None
    after_heading = m.end()
    next_iter = re.search(r"^## iteration-\d+", content[after_heading:], re.M)
    if next_iter:
        return after_heading + next_iter.start()
    return len(content)


def _append_for_iteration(path: Path, iteration_id: str, text: str) -> None:
    """
    Append summary blocks for one iteration, keeping them under that iteration's section.

    On a fresh experiment each iteration heading is written once and blocks are
    appended at EOF (chronological). When an experiment is **continued** and an
    iteration is re-run (e.g. ``iteration-007`` failed first, then succeeded on
    retry), the heading already exists but old blocks must not be orphaned at the
    end of the file — new blocks are inserted at the end of that iteration's
    section (before the next ``## iteration-…`` heading).
    """
    iid = normalize_iteration_id(iteration_id)
    block = text if text.endswith("\n") else text + "\n"

    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"## {iid}\n\n{block}", encoding="utf-8")
        return

    content = path.read_text(encoding="utf-8")
    if not _iteration_heading_present(path, iid):
        prefix = "\n" if content and not content.endswith("\n\n") else ""
        _append(path, f"{prefix}## {iid}\n\n{block}")
        return

    insert_at = _insert_pos_for_iteration_section(content, iid)
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


def _spec_diff_source_label(spec_path: Path) -> str:
    """Human label for the iteration whose spec we diff against (not ``03-spec/``)."""
    # spec.yaml lives at ``iteration-NNN-…/03-spec/spec.yaml``
    iteration_dir = spec_path.parent.parent
    if iteration_dir.name.startswith("iteration-"):
        return f"`{iteration_dir.name}`"
    return f"`{spec_path.parent.name}`"


def _extract_llm_narrative(raw_response: str) -> str:
    """Text before the machine-readable SPEC / YAML block."""
    raw = (raw_response or "").strip()
    if not raw:
        return "(no LLM narrative in response.log)"

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

    if re.match(r"^<SPEC>", raw, re.I) or raw.lstrip().startswith("backend:"):
        return (
            "(no prose rationale — model returned only the machine-readable spec; "
            "see **Deployment** and **Changes vs** above.)"
        )

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
        f"- **Backend web_concurrency**: {b.web_concurrency}",
        f"- **Backend worker**: `{b.worker_class}`"
        + (f" × {b.worker_threads} threads" if b.worker_threads else ""),
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
            "backend web_concurrency",
            prev.backend.web_concurrency,
            cur.backend.web_concurrency,
        ),
        _diff_field("backend worker_class", prev.backend.worker_class, cur.backend.worker_class),
        _diff_optional_int(
            "backend worker_threads",
            prev.backend.worker_threads,
            cur.backend.worker_threads,
        ),
        _diff_field("backend preload", prev.backend.preload, cur.backend.preload),
        _diff_optional_int("backend backlog", prev.backend.backlog, cur.backend.backlog),
        _diff_optional_int(
            "backend max_requests", prev.backend.max_requests, cur.backend.max_requests
        ),
        _diff_optional_int(
            "backend max_requests_jitter",
            prev.backend.max_requests_jitter,
            cur.backend.max_requests_jitter,
        ),
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
                "p95_logged": m.group("p95_logged"),
                "reqs": int(m.group("reqs")),
                "fail": int(m.group("fail")),
                "fail_pct": m.group("fail_pct"),
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
        from .plots.ramp_data import load_stats_timeseries

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
        from .plots.ramp_data import peak_goodput_from_bench_log, sustained_goodput_from_bench

        if perf_run_dir is not None:
            sustained = sustained_goodput_from_bench(perf_run_dir)
            if sustained is not None and sustained.goodput_rps > 0:
                rows.append(
                    f"**Sustained max goodput ({sustained.window_s}s window)**: "
                    f"**{sustained.goodput_rps:.1f}** succ/s @ {sustained.users}u "
                    f"(t={sustained.t_s}s, fail={sustained.fail_pct:.1f}%, "
                    f"drift={sustained.drift_pct:.1f}%) — primary experiment metric."
                )
        run_peak, peak_users = peak_goodput_from_bench_log(log_text)
        if run_peak > 0:
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
    Append a baseline-codegen subsection under ``iteration-000``.

    Renders one line per attempt (status + FT pass counts + error excerpt) so
    a reader can see at a glance how many tries it took to get the application
    code to pass the functional test suite — and which attempts' transcripts
    live under ``02-code/attempts/<NNN>/`` for forensics on the failures.
    """
    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id("iteration-000")

    from .workspace import (
        baseline_codegen_meta_path as _meta_path,
        iteration_code_attempts_dir as _attempts_dir,
    )

    attempt_blocks: list[str] = []
    meta_path = _meta_path(iteration_path)
    attempts_data: list[dict[str, Any]] = []
    if meta_path.is_file():
        try:
            payload = (
                __import__("json").loads(meta_path.read_text(encoding="utf-8"))
            )
            if isinstance(payload, dict):
                raw = payload.get("attempts")
                if isinstance(raw, list):
                    attempts_data = [a for a in raw if isinstance(a, dict)]
        except Exception:
            attempts_data = []

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
        line = f"- **Attempt {idx}**: `{st}`{infra_tag} ({ft_part})"
        if err:
            err_excerpt = err if len(err) <= 200 else err[:200].rstrip() + "…"
            line += f" — {err_excerpt}"
        line += f" → `{_attempts_dir(iteration_path)}/{int(idx):03d}/`"
        attempt_blocks.append(line)

        # Inline a short tail of test.log on infra failures so the operator
        # sees the docker/port error directly in the summary without having
        # to dig into the per-attempt directory.
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

    body = "\n".join(
        [
            f"### Baseline code generation ({_utc_now_label()})",
            "",
            f"- **Mode**: `regenerate` (baseline LLM codegen + FT gate)",
            f"- **Status**: `{status}`"
            + (f" (winning attempt: **{winning_attempt}**)" if winning_attempt else ""),
            f"- **Attempts used**: {attempts_used} / {max_attempts}",
            f"- **Model**: `{task.model}` (provider `{task.provider}`, "
            f"temperature {task.temperature})",
            f"- **Code path**: `{iteration_path / '02-code' / 'code'}`",
            "",
            "**Attempts**" if attempt_blocks else "**Attempts**: (none recorded)",
            "",
            *attempt_blocks,
            "",
            *(
                [
                    "**Failure reason**"
                    + (" — host environment issue (no LLM retries spent)" if status == "infra_failed" or any_infra else ""),
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
    _append_for_iteration(path, iid, body)
    return path


def _build_spec_generation_block_text(
    *,
    iteration_path: Path,
    spec: K8sWorkloadSpec,
    raw_response: str,
    warnings: list[str],
    had_prior_feedback: bool,
    iteration_index: int,
) -> str:
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

    narrative = _extract_llm_narrative(raw_response)
    warn_block = ""
    if warnings:
        warn_block = "\n**Validation warnings**\n" + "\n".join(f"- {w}" for w in warnings) + "\n"

    return "\n".join(
        [
            f"### Spec generation ({_utc_now_label()})",
            "",
            f"- **Iteration index**: {iteration_index}",
            f"- **Prior Locust feedback in prompt**: {'yes' if had_prior_feedback else 'no (first iteration)'}",
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
            "**LLM rationale** (from `response.log`, text before `<SPEC>`)",
            "",
            narrative,
            warn_block,
            "",
            "---",
            "",
        ]
    )


def append_spec_generation_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    spec: K8sWorkloadSpec,
    raw_response: str,
    warnings: list[str],
    had_prior_feedback: bool,
    iteration_index: int,
    load_profile: str | None = None,
) -> Path:
    """Append spec-generation subsection for one iteration."""
    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)

    body = _build_spec_generation_block_text(
        iteration_path=iteration_path,
        spec=spec,
        raw_response=raw_response,
        warnings=warnings,
        had_prior_feedback=had_prior_feedback,
        iteration_index=iteration_index,
    )
    _append_for_iteration(path, iid, body)
    return path


def _build_perf_run_block_text(
    *,
    perf_run_dir: Path,
    feedback: IterationFeedback | None = None,
) -> str:
    """Markdown body for one iteration's ``### Locust run`` subsection."""
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
        phases,
        initial_users=initial_users,
        v2_stop=v2_stop,
        log_text=log_text,
        perf_run_dir=perf_run_dir,
    )
    locust_line = _aggregate_locust_line(perf_run_dir)

    fb = feedback
    if fb is None:
        from .workspace import load_feedback

        fb = load_feedback(perf_run_dir)

    locust_table = ""
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

    diag_hint = _format_diagnostics_metrics_for_summary(perf_run_dir)

    notes_line = ""
    if fb and fb.notes and fb.notes.strip():
        notes_line = f"\n**Notes**\n\n{fb.notes.strip()}\n"

    return "\n".join(
        [
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
            diag_hint,
            notes_line,
            "",
            "---",
            "",
        ]
    )


def _replace_locust_run_block(
    content: str,
    iteration_id: str,
    new_block: str,
) -> tuple[str, bool]:
    """Replace the ``### Locust run`` subsection under one iteration heading."""
    iid = normalize_iteration_id(iteration_id)
    heading_re = re.compile(rf"^## {re.escape(iid)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        return content, False

    sec_start = heading.end()
    next_iter = re.search(r"^## iteration-\d+", content[sec_start:], re.M)
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
    iteration_id: str,
    new_block: str,
) -> tuple[str, bool]:
    """Replace the ``### Spec generation`` subsection under one iteration heading."""
    iid = normalize_iteration_id(iteration_id)
    heading_re = re.compile(rf"^## {re.escape(iid)}\s*$", re.M)
    heading = heading_re.search(content)
    if not heading:
        return content, False

    sec_start = heading.end()
    next_iter = re.search(r"^## iteration-\d+", content[sec_start:], re.M)
    sec_end = sec_start + next_iter.start() if next_iter else len(content)
    section = content[sec_start:sec_end]

    block_re = re.compile(r"^### Spec generation \([^)]*\)\n.*?\n---\n", re.M | re.S)
    block = block_re.search(section)
    if not block:
        return content, False

    new_section = section[: block.start()] + new_block + section[block.end() :]
    new_content = content[:sec_start] + new_section + content[sec_end:]
    return new_content, True


def regenerate_experiment_summary_spec_blocks(experiment_root: Path) -> Path:
    """Rebuild every ``### Spec generation`` subsection from on-disk specs + response logs."""
    from .workspace import (
        ITERATIONS_DIRNAME,
        iteration_folder_is_failed,
        iteration_spec_dir,
        parse_iteration_index,
    )
    from .workspace.artifacts import RESPONSE_LOG_FILENAME

    experiment_path = experiment_root.expanduser().resolve()
    if (experiment_path / ITERATIONS_DIRNAME).is_dir():
        root = experiment_path
    else:
        from .workspace import k8s_workspace_root

        root = k8s_workspace_root(experiment_path)
    path = experiment_summary_path(root)
    if not path.is_file():
        return path

    content = path.read_text(encoding="utf-8")
    iterations_dir = root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return path

    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        spec_path = find_iteration_spec_path(child)
        if spec_path is None:
            continue
        try:
            spec = K8sWorkloadSpec.from_yaml_file(spec_path)
        except ValueError:
            continue
        response_path = iteration_spec_dir(child) / RESPONSE_LOG_FILENAME
        raw_response = ""
        if response_path.is_file():
            raw_response = response_path.read_text(encoding="utf-8", errors="replace")

        iid = f"iteration-{idx:03d}"
        block = _build_spec_generation_block_text(
            iteration_path=child,
            spec=spec,
            raw_response=raw_response,
            warnings=[],
            had_prior_feedback=idx > 0,
            iteration_index=idx,
        )
        updated_content, replaced = _replace_spec_generation_block(content, iid, block)
        if replaced:
            content = updated_content
        else:
            _append_for_iteration(path, iid, block)
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
    from .workspace import (
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
        from .workspace import k8s_workspace_root

        root = k8s_workspace_root(experiment_path)
    path = experiment_summary_path(root)
    if not path.is_file():
        _ensure_header(path, sample_dir=root, load_profile=load_profile)

    content = path.read_text(encoding="utf-8")
    iterations_dir = root / ITERATIONS_DIRNAME
    if not iterations_dir.is_dir():
        return path

    for child in sorted(iterations_dir.iterdir()):
        if not child.is_dir() or iteration_folder_is_failed(child.name):
            continue
        idx = parse_iteration_index(child.name)
        if idx is None:
            continue
        bench_dir = iteration_bench_dir(child)
        if not (bench_dir / "bench.log").is_file():
            continue

        iid = f"iteration-{idx:03d}"
        block = _build_perf_run_block_text(
            perf_run_dir=bench_dir,
            feedback=load_feedback(bench_dir),
        )
        updated_content, replaced = _replace_locust_run_block(content, iid, block)
        if replaced:
            content = updated_content
        else:
            _append_for_iteration(path, iid, block)
            content = path.read_text(encoding="utf-8")

    path.write_text(content, encoding="utf-8")
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
    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)

    body = _build_perf_run_block_text(perf_run_dir=perf_run_dir, feedback=feedback)
    _append_for_iteration(path, iid, body)
    return path


def _format_pod_utilization_for_summary(text: str) -> str:
    """Render the ``pod_utilization`` payload as a collapsible Markdown section."""
    body = (text or "").strip()
    if not body:
        return ""
    if "unavailable" in body.splitlines()[0].lower():
        return f"\n**K8s utilization**: {body.splitlines()[0]}\n"
    return _collapsible_details("K8s utilization (kubectl top during the run)", body)


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
    """Condensed Postgres / pooler / cache / replication metrics from the bench run."""
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

    sections: list[str] = []
    for title, block in (
        ("PostgreSQL", diag.database.to_prompt_block()),
        ("Replication", diag.replication.to_prompt_block()),
        ("PgBouncer pools", diag.pooler.to_prompt_block()),
        ("Redis cache", diag.cache.to_prompt_block()),
        ("Pod health", diag.pod_health.to_prompt_block()),
    ):
        text = (block or "").strip()
        if not text or text.startswith("(no "):
            continue
        sections.append(f"#### {title}\n\n{text}")

    if diag.pod_errors.sources:
        log_lines = [
            "#### Pod log warnings/errors",
            "",
            "| Source | Lines | Warnings | Errors |",
            "|---|---:|---:|---:|",
        ]
        for st in diag.pod_errors.sources:
            if st.warnings or st.errors:
                log_lines.append(
                    f"| {st.source} | {st.lines_total} | {st.warnings} | {st.errors} |"
                )
        if len(log_lines) > 4:
            sections.append("\n".join(log_lines))

    if not sections:
        return ""

    return _collapsible_details(
        "Run diagnostics (Postgres, pooler, cache, replication)",
        "\n\n".join(sections),
    )


def append_iteration_failure_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    iteration_path: Path,
    failure_reason: str,
    kind: str,
    error_excerpt: str = "",
    load_profile: str | None = None,
) -> Path:
    """
    Append a failure narrative for an iteration that never produced bench data.

    Renders failure stage, reason, error excerpt, and the spec that was attempted
    (when present on disk). When a structured ``failure_report.json`` exists for
    the iteration, the block also lists which functional tests passed / failed
    and surfaces an explicit **Infrastructure failure** banner when the FT run
    was blocked by the test harness rather than the application.
    """
    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)

    # Load the structured FT report (if any) so we can render the *real* cause
    # — infrastructure-failure banner, per-test outcome list — instead of
    # leaving the reader to guess from the truncated Python traceback.
    from .workspace import load_failure_report

    report = None
    if kind == "code":
        try:
            report = load_failure_report(iteration_path)
        except Exception:
            report = None

    stage_label = (
        "baseline spec (manifest could not be deployed)"
        if kind == "baseline"
        else "spec deployment (manifest could not be deployed)"
        if kind == "spec"
        else "code refinement (functional tests did not pass)"
        if kind == "code"
        else kind or "iteration"
    )

    body_lines: list[str] = [
        f"### Iteration failure ({_utc_now_label()})",
        "",
        "- **Status**: `failed` (no benchmark data produced)",
        f"- **Stage**: {stage_label}",
        f"- **Reason**: {failure_reason or '(no reason recorded)'}",
        f"- **Folder**: `{iteration_path.name}`",
    ]

    if report is not None and report.is_infrastructure_failure:
        infra = report.infrastructure_failure
        assert infra is not None
        body_lines.extend(
            [
                "- **Cause**: **Infrastructure failure (not an application "
                f"bug)** — `{infra.kind}`: {infra.description}",
            ]
        )

    if report is not None:
        body_lines.append(
            f"- **Functional tests**: {report.num_passed_ft}/"
            f"{report.num_total_ft} passed"
        )

    body_lines.append("")

    if report is not None and report.is_infrastructure_failure:
        infra = report.infrastructure_failure
        assert infra is not None
        body_lines.extend(
            [
                "**Infrastructure failure**",
                "",
                "The functional-test harness could not start the application or "
                "its database container, so **no functional test actually "
                "exercised the application code**. Treat the test outcome below "
                "as *blocked*, not *failing*. Rewriting the application will "
                "not help — investigate the test host (port collisions, leftover "
                "Docker containers, image availability).",
                "",
            ]
        )
        if infra.evidence:
            body_lines.extend(
                [
                    "**Evidence (raw log line)**",
                    "",
                    "```",
                    infra.evidence,
                    "```",
                    "",
                ]
            )

    if report is not None and (report.failed_tests or report.passed_tests):
        body_lines.append("**Per-test outcome**")
        body_lines.append("")
        for ft in report.failed_tests:
            label = "blocked" if report.is_infrastructure_failure else "failed"
            body_lines.append(f"- `{ft.name}` — **{label}**")
        for name in report.passed_tests:
            body_lines.append(f"- `{name}` — passed")
        body_lines.append("")

    # Detailed application-level evidence (per failed test) when we are sure
    # the failure is *not* infrastructure — otherwise the per-test excerpts
    # just repeat the same Docker traceback five times.
    if report is not None and report.failed_tests and not report.is_infrastructure_failure:
        body_lines.append("**Failed-test evidence**")
        body_lines.append("")
        for ft in report.failed_tests:
            body_lines.append(f"- `{ft.name}`")
            snippet = ft.per_test_log_tail.strip() or ft.container_error_excerpt.strip()
            if snippet:
                if len(snippet) > 600:
                    snippet = snippet[:600].rstrip() + "\n…(truncated)"
                body_lines.append("  ```")
                body_lines.extend("  " + l for l in snippet.splitlines())
                body_lines.append("  ```")
        body_lines.append("")

    excerpt = (error_excerpt or "").strip()
    if excerpt:
        if len(excerpt) > _MAX_NARRATIVE_CHARS:
            excerpt = excerpt[:_MAX_NARRATIVE_CHARS].rstrip() + "\n…(truncated)"
        body_lines.extend(
            ["**Error excerpt**", "", "```", excerpt, "```", ""]
        )

    spec_file = find_iteration_spec_path(iteration_path)
    if spec_file is not None and spec_file.is_file():
        try:
            spec = K8sWorkloadSpec.from_yaml_file(spec_file)
            body_lines.append("**Spec that was attempted**")
            body_lines.append("")
            body_lines.extend(_spec_bullets(spec))
            body_lines.append("")
        except Exception:
            # Spec validation may fail on the same broken values that caused
            # the failure (e.g. negative cpu); skip the bullet block rather
            # than crash the summary writer.
            pass

    body_lines.extend(["---", ""])
    _append_for_iteration(path, iid, "\n".join(body_lines))
    return path


def append_refinement_decision_block(
    *,
    sample_dir: Path,
    iteration_id: str,
    decision: Any,
    load_profile: str | None = None,
) -> Path:
    """Append deployment-vs-code decision before a phase's spec generation."""
    path = experiment_summary_path(sample_dir)
    _ensure_header(path, sample_dir=sample_dir, load_profile=load_profile)
    iid = normalize_iteration_id(iteration_id)
    body = "\n".join(
        [
            f"### Refinement decision (iteration {decision.iteration_index})",
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
    _append_for_iteration(path, iid, body)
    return path

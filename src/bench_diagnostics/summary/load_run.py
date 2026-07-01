"""Extract a compact adaptive-ramp narrative from ``bench.log``."""

from __future__ import annotations

import re
from typing import Any

_ADAPTIVE_PHASE_RE = re.compile(
    r"adaptive phase end t=(\d+)s: (?P<action>.*?) \| "
    r"reqs=(?P<reqs>\d+) fail=(?P<fail>\d+) \((?P<fail_pct>[^)]+)\) "
    r"p\d+=(?P<p95_logged>\S+)"
)
_WARMUP_END_RE = re.compile(
    r"adaptive-v2 warmup end t=(\d+)s at users=(\d+) \| "
    r"reqs=(\d+) fail=(\d+) \(([^)]+)\) p\d+=(\S+)"
)
_ADAPTIVE_V2_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) "
)
_STEP_GOODPUT_RE = re.compile(r"step_goodput=([\d.]+)/s")
_NEXT_USERS_RE = re.compile(r"-> users=(\d+)")


def load_profile_from_config(config: dict) -> str:
    profiles = config.get("requested_profiles") or {}
    if isinstance(profiles, dict):
        prof = profiles.get("load_profile")
        if prof:
            return str(prof)
    resolved = config.get("resolved_load_profile")
    if isinstance(resolved, dict):
        name = resolved.get("name")
        if name:
            return str(name)
    if isinstance(resolved, str):
        return resolved
    return str(config.get("load_profile") or "")


def _shorten_action(action: str) -> str:
    text = action.strip()
    if text.startswith("plateau"):
        return "plateau (stop)"
    if "warmup" in text.lower():
        return "warmup complete"
    if "goodput ramp" in text:
        return "increase load"
    m = _NEXT_USERS_RE.search(text)
    if m:
        return f"hold at {m.group(1)} users"
    if len(text) > 48:
        return text[:45] + "…"
    return text


def _parse_warmup(bench_log: str) -> dict[str, Any] | None:
    for line in bench_log.splitlines():
        m = _WARMUP_END_RE.search(line)
        if not m:
            continue
        return {
            "t_s": int(m.group(1)),
            "users": int(m.group(2)),
            "reqs": int(m.group(3)),
            "fail": int(m.group(4)),
            "fail_pct": m.group(5),
            "p95_ms": m.group(6).rstrip("ms"),
        }
    return None


def _parse_phases(bench_log: str) -> list[dict[str, Any]]:
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
        action = m.group("action").strip()
        next_m = _NEXT_USERS_RE.search(action)
        goodput_m = _STEP_GOODPUT_RE.search(line)
        phases.append(
            {
                "t_s": t_s,
                "next_users": int(next_m.group(1)) if next_m else None,
                "p95_ms": m.group("p95_logged").rstrip("ms"),
                "fail_pct": m.group("fail_pct"),
                "step_goodput_rps": float(goodput_m.group(1)) if goodput_m else None,
                "action": _shorten_action(action),
            }
        )
    phases.sort(key=lambda p: p["t_s"])
    return phases


def _parse_v2_stop_reason(bench_log: str) -> str | None:
    reason: str | None = None
    for line in bench_log.splitlines():
        m = _ADAPTIVE_V2_STOP_RE.search(line)
        if m:
            reason = m.group("reason")
    return reason


def summarize_load_run(
    bench_log: str,
    *,
    load_profile: str = "",
) -> str:
    """Summarize an adaptive bench run using controller decision points only."""
    del load_profile

    if not bench_log.strip():
        return "(no bench.log available)"

    warmup = _parse_warmup(bench_log)
    phases = _parse_phases(bench_log)
    stop_reason = _parse_v2_stop_reason(bench_log)

    if not warmup and not phases:
        return "(no adaptive ramp decisions found in bench.log)"

    lines = [
        "| t (s) | users | → next | goodput/s | p95 ms | fail % | decision |",
        "|---:|---:|---:|---:|---:|---:|---|",
    ]

    level_users: int | None = None
    peak_gp = 0.0
    peak_gp_t: int | None = None

    if warmup:
        level_users = int(warmup["users"])
        lines.append(
            f"| {warmup['t_s']} | {warmup['users']} | — | — | {warmup['p95_ms']} | "
            f"{warmup['fail_pct']} | warmup end |"
        )

    for p in phases:
        gp_val = p.get("step_goodput_rps")
        gp = f"{gp_val:.1f}" if gp_val is not None else "—"
        if gp_val is not None and gp_val >= peak_gp:
            peak_gp = gp_val
            peak_gp_t = int(p["t_s"])
        next_users = p.get("next_users")
        user_cell = str(level_users) if level_users is not None else "—"
        next_cell = str(next_users) if next_users is not None else "—"
        lines.append(
            f"| {p['t_s']} | {user_cell} | {next_cell} | {gp} | {p['p95_ms']} | "
            f"{p['fail_pct']} | {p['action']} |"
        )
        if next_users is not None:
            level_users = next_users

    if peak_gp > 0:
        lines.append("")
        lines.append(
            f"**Peak step goodput**: {peak_gp:.1f}/s at t={peak_gp_t}s"
            + (f"; stop reason=`{stop_reason}`" if stop_reason else "")
        )

    return "\n".join(lines)

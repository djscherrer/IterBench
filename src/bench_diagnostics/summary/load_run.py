"""Extract a compact adaptive-ramp narrative from ``bench.log``."""

from __future__ import annotations

import re
from typing import Any

from .adaptive_log import (
    ADAPTIVE_PHASE_RE,
    ADAPTIVE_V2_STOP_RE,
    WARMUP_END_RE,
    classify_adaptive_run_outcome,
    parse_adaptive_stop,
    parse_explore_peak,
    phase_fail_pct,
    phase_p95_token,
    phase_roll_goodput,
)

_STEP_GOODPUT_RE = re.compile(r"step_goodput=([\d.]+)/s")
_USERS_RE = re.compile(r"users=(\d+)")
_EXPLORE_END_REASON_RE = re.compile(r"explore end \((?P<reason>[^)]*)\)")
_EXPLORE_PEAK_RE = re.compile(r"peak=(?P<users>\d+)u/(?P<goodput>[\d.]+)/s")
_RECOVERY_ATTEMPT_RE = re.compile(
    r"recovery settle attempt=(?P<attempt>\d+).*?users=(?P<users>\d+)"
)
_RECOVERY_RETRY_RE = re.compile(
    r"recovery retry attempt=(?P<attempt>\d+)/(?P<max>\d+).*?-> users=(?P<users>\d+)"
)
_WARMUP_INLINE_GOODPUT_RE = re.compile(r"goodput=([\d.]+)/s")
_ADAPTIVE_SAMPLE_RE = re.compile(
    r"(?:adaptive )?sample t=(\d+)s users=(\d+) goodput=([\d.]+)/s "
    r"fail_pct=([\d.]+)% p95=([\d.]+)ms"
)


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


def _fmt_p95(token: str | None) -> str:
    if token is None or token in ("", "—", "n/a", "None"):
        return "n/a"
    text = str(token).strip()
    if text.endswith("ms"):
        return text
    return f"{text}ms"


def _fmt_fail(token: str | None) -> str:
    if token is None or token in ("", "—", "n/a", "None"):
        return "n/a"
    text = str(token).strip().rstrip("%")
    return f"{text}%"


def _fmt_stats(
    *,
    users: int | None = None,
    goodput: float | None = None,
    fail_pct: str | None = None,
    p95: str | None = None,
) -> str:
    parts: list[str] = []
    if users is not None:
        parts.append(f"users={users}")
    if goodput is not None:
        parts.append(f"goodput={goodput:.1f}/s")
    parts.append(f"fail%={_fmt_fail(fail_pct)}")
    parts.append(f"p95={_fmt_p95(p95)}")
    return ", ".join(parts)


def _humanize_explore_reason(reason: str) -> str:
    text = reason.strip()
    low = text.lower()
    if "goodput=" in low and "peak=" in low:
        return f"goodput collapse ({text})"
    if low.startswith("p95=") or ">ms" in low.replace(" ", "") or "p95=" in low:
        return f"p95 overload ({text})"
    if "fail%" in low or low.startswith("fail"):
        return f"fail% threshold ({text})"
    if "plateau" in low:
        return f"goodput plateau ({text})"
    if "max users" in low:
        return f"max users reached ({text})"
    return text or "explore stop"


def _parse_warmup(bench_log: str) -> dict[str, Any] | None:
    for line in bench_log.splitlines():
        m = WARMUP_END_RE.search(line)
        if not m:
            continue
        healthy = True
        if "healthy=False" in line or "warmup unhealthy" in line:
            healthy = False
        goodput = phase_roll_goodput(m)
        if goodput is None:
            gm = _WARMUP_INLINE_GOODPUT_RE.search(line)
            if gm:
                goodput = float(gm.group(1))
        return {
            "t_s": int(m.group(1)),
            "users": int(m.group(2)),
            "healthy": healthy,
            "goodput_rps": goodput,
            "fail_pct": phase_fail_pct(m),
            "p95_ms": phase_p95_token(m),
        }
    return None


def _parse_phase_ends(bench_log: str) -> list[dict[str, Any]]:
    seen_t: set[int] = set()
    phases: list[dict[str, Any]] = []
    for line in bench_log.splitlines():
        if "adaptive phase end" not in line:
            continue
        m = ADAPTIVE_PHASE_RE.search(line)
        if not m:
            continue
        t_s = int(m.group(1))
        if t_s in seen_t:
            continue
        seen_t.add(t_s)
        action = m.group("action").strip()
        goodput_m = _STEP_GOODPUT_RE.search(line)
        users_m = _USERS_RE.search(action)
        roll_gp = phase_roll_goodput(m)
        step_gp = float(goodput_m.group(1)) if goodput_m else None
        phases.append(
            {
                "t_s": t_s,
                "action": action,
                "users": int(users_m.group(1)) if users_m else None,
                "p95_ms": phase_p95_token(m),
                "fail_pct": phase_fail_pct(m),
                "goodput_rps": step_gp if step_gp is not None else roll_gp,
                "raw": line,
            }
        )
    phases.sort(key=lambda p: p["t_s"])
    return phases


def _max_users_before(bench_log: str, t_s: int) -> int | None:
    max_u: int | None = None
    for line in bench_log.splitlines():
        m = _ADAPTIVE_SAMPLE_RE.search(line)
        if not m:
            continue
        sample_t = int(m.group(1))
        if sample_t > t_s:
            break
        users = int(m.group(2))
        if max_u is None or users > max_u:
            max_u = users
    return max_u


def _render_warmup(warmup: dict[str, Any] | None) -> list[str]:
    lines = ["#### Warmup"]
    if warmup is None:
        lines.append("- (no warmup end line found)")
        return lines
    status = "succeeded" if warmup.get("healthy", True) else "failed (unhealthy)"
    lines.append(
        f"- {status.capitalize()} at t={warmup['t_s']}s: "
        + _fmt_stats(
            users=int(warmup["users"]),
            goodput=warmup.get("goodput_rps"),
            fail_pct=warmup.get("fail_pct"),
            p95=warmup.get("p95_ms"),
        )
    )
    return lines


def _render_explore(
    phases: list[dict[str, Any]],
    *,
    bench_log: str,
    stop: dict[str, str] | None,
) -> list[str]:
    lines = ["#### Explore"]
    explore = next((p for p in phases if "explore end" in p["action"]), None)
    peak_users, peak_gp = parse_explore_peak(bench_log)
    if explore is None and peak_gp is None:
        lines.append("- (explore did not complete / no explore-end line)")
        return lines

    if peak_gp is not None:
        peak_bit = f"{peak_gp:.1f}/s"
        if peak_users is not None:
            peak_bit += f" at {peak_users} users"
        lines.append(
            f"- Peak goodput achieved: {peak_bit} "
            "(not sustained afterwards)."
        )

    if explore is None:
        return lines

    reason_m = _EXPLORE_END_REASON_RE.search(explore["action"])
    reason = _humanize_explore_reason(reason_m.group("reason") if reason_m else "")
    end_users = explore.get("users")
    high_bad = None
    if stop and stop.get("high_bad") not in (None, "None", "n/a"):
        try:
            high_bad = int(float(stop["high_bad"]))
        except (TypeError, ValueError):
            high_bad = None
    users_at_stop = high_bad or _max_users_before(bench_log, int(explore["t_s"]))

    end_line = (
        f"- Ended at t={explore['t_s']}s"
        + (f" after rising to {users_at_stop} users" if users_at_stop else "")
        + ": "
        + _fmt_stats(
            users=None,
            goodput=explore.get("goodput_rps"),
            fail_pct=explore.get("fail_pct"),
            p95=explore.get("p95_ms"),
        )
    )
    if end_users is not None:
        end_line += f". Dropped to recovery floor users={end_users}."
    lines.append(end_line)
    lines.append(f"- Reason explore ended: {reason}")
    return lines


def _render_recovery(phases: list[dict[str, Any]]) -> list[str]:
    lines = ["#### Recovery"]

    handoff_attempt: str | None = None
    handoff_users: str | None = None
    for p in phases:
        if "explore end" in p["action"] and "recovery settle" in p["action"]:
            m = _RECOVERY_ATTEMPT_RE.search(p["action"])
            if m:
                handoff_attempt = m.group("attempt")
                handoff_users = m.group("users")
            break

    attempts: list[str] = []
    saw_terminal = False
    for p in phases:
        action = p["action"]
        stats = _fmt_stats(
            users=p.get("users"),
            goodput=p.get("goodput_rps"),
            fail_pct=p.get("fail_pct"),
            p95=p.get("p95_ms"),
        )
        if "recovery retry" in action:
            m = _RECOVERY_RETRY_RE.search(action)
            failed_n = (
                str(int(m.group("attempt")) - 1)
                if m
                else (handoff_attempt or "?")
            )
            failed_users = handoff_users or "previous"
            attempts.append(
                f"- Attempt {failed_n} at {failed_users} users: failed health "
                f"check at t={p['t_s']}s ({stats})."
            )
            if m:
                attempts.append(
                    f"- Attempt {m.group('attempt')}/{m.group('max')} at "
                    f"{m.group('users')} users: settling after drop."
                )
                handoff_attempt = m.group("attempt")
                handoff_users = m.group("users")
        elif "recovery healthy" in action:
            n = handoff_attempt or "1"
            u = handoff_users or (
                str(p["users"]) if p.get("users") is not None else "?"
            )
            attempts.append(
                f"- Attempt {n} at {u} users: succeeded at t={p['t_s']}s "
                f"({stats}) → entered refine."
            )
            saw_terminal = True
        elif "recovery gave up" in action or (
            "recovery unhealthy" in action and "gave up" in action
        ):
            n = handoff_attempt or "?"
            u = handoff_users or (
                str(p["users"]) if p.get("users") is not None else "?"
            )
            attempts.append(
                f"- Attempt {n} at {u} users: gave up at t={p['t_s']}s "
                f"({stats}). Refine not reached."
            )
            saw_terminal = True

    if not attempts:
        if handoff_users is not None:
            lines.append(
                f"- Attempt {handoff_attempt or '1'} at {handoff_users} users: "
                "started settle; no recovery outcome line found."
            )
        elif any("refine" in p["action"] for p in phases):
            lines.append("- (no explicit recovery lines; refine started anyway)")
        else:
            lines.append("- (recovery phase not reached)")
        return lines

    if (
        not saw_terminal
        and handoff_users is not None
        and not any("Attempt" in a and "succeeded" in a for a in attempts)
    ):
        # Keep handoff visible when only retries were logged without terminal.
        pass

    lines.extend(attempts)
    return lines

def _render_refine(
    phases: list[dict[str, Any]],
    *,
    sustained_goodput_rps: float | None,
    sustained_users: int | None,
    stop_reason: str | None,
) -> list[str]:
    lines = ["#### Refine"]
    refine_phases = [
        p
        for p in phases
        if p["action"].startswith("refine")
        or "refine baseline" in p["action"]
        or "refine ramp" in p["action"]
        or "refine hold" in p["action"]
        or "refine stall" in p["action"]
        or "refine overload" in p["action"]
    ]
    if not refine_phases:
        lines.append("- (refine phase not reached)")
        return lines

    for p in refine_phases:
        action = p["action"]
        # For refine ramps, step_goodput was settled at (after - delta) users;
        # the action's ``users=`` is the post-bump target.
        settle_users = p.get("users")
        after_users = None
        step_users = None
        move_m = re.search(r"-> \+(\d+) users=(\d+)", action)
        if move_m and "stall" not in action and "overload" not in action.lower():
            step_users = int(move_m.group(1))
            after_users = int(move_m.group(2))
            settle_users = max(0, after_users - step_users)

        stats = _fmt_stats(
            users=settle_users,
            goodput=p.get("goodput_rps"),
            fail_pct=p.get("fail_pct"),
            p95=p.get("p95_ms"),
        )
        if "baseline" in action:
            move = ""
            if "-> ramp" in action:
                raw_move = action.split("->", 1)[1].strip()
                um = re.search(r"ramp \+(\d+) users=(\d+)", raw_move)
                if um:
                    move = f" → ramp +{um.group(1)} to {um.group(2)} users"
                else:
                    move = " → " + raw_move
            lines.append(f"- t={p['t_s']}s baseline: {stats}{move}.")
        elif "stall" in action:
            lines.append(f"- t={p['t_s']}s stop (goodput stall): {stats}.")
        elif "overload" in action.lower():
            lines.append(f"- t={p['t_s']}s overload backoff: {stats}.")
        elif "hold" in action:
            lines.append(f"- t={p['t_s']}s hold: {stats}.")
        elif "ramp" in action:
            move = ""
            if step_users is not None and after_users is not None:
                move = f" → +{step_users} to {after_users} users"
            else:
                step_m = re.search(r"-> users=(\d+) step=(\d+)", action)
                if step_m:
                    move = f" → +{step_m.group(2)} to {step_m.group(1)} users"
                elif "-> users=" in action:
                    move = " → " + action.split("->", 1)[1].strip()
            lines.append(f"- t={p['t_s']}s settled: {stats}{move}.")
        else:
            short = action if len(action) <= 90 else action[:87] + "…"
            lines.append(f"- t={p['t_s']}s: {stats}. ({short})")

    if sustained_goodput_rps is not None:
        users_bit = (
            f" at {sustained_users} users" if sustained_users is not None else ""
        )
        lines.append("")
        lines.append(
            f"**Reported goodput (best settled refine level): "
            f"{sustained_goodput_rps:.0f}/s{users_bit}.**"
        )
        if stop_reason:
            lines.append(f"Stop reason=`{stop_reason}`.")
    elif stop_reason:
        lines.append("")
        lines.append(f"Stop reason=`{stop_reason}`.")
    return lines


def summarize_load_run(
    bench_log: str,
    *,
    load_profile: str = "",
    sustained_goodput_rps: float | None = None,
    sustained_users: int | None = None,
) -> str:
    """Summarize an adaptive bench run as a phase-by-phase narrative."""
    del load_profile

    if not bench_log.strip():
        return "(no bench.log available)"

    warmup = _parse_warmup(bench_log)
    phases = _parse_phase_ends(bench_log)
    stop = parse_adaptive_stop(bench_log)
    stop_reason = stop.get("reason") if stop else None
    if stop_reason is None:
        # Fall back to last adaptive-v2 stop parse helper if present elsewhere.
        for line in bench_log.splitlines():
            m = ADAPTIVE_V2_STOP_RE.search(line)
            if m:
                stop_reason = m.group("reason")

    outcome = classify_adaptive_run_outcome(
        bench_log,
        sustained_goodput_rps=sustained_goodput_rps,
        sustained_users=sustained_users,
    )

    if not warmup and not phases and outcome is None:
        return "(no adaptive ramp decisions found in bench.log)"

    blocks: list[str] = []
    if outcome is not None:
        blocks.append(outcome.feedback_block())
        blocks.append("")

    blocks.append("Phase narrative:")
    blocks.append("")
    blocks.extend(_render_warmup(warmup))
    blocks.append("")
    blocks.extend(
        _render_explore(phases, bench_log=bench_log, stop=stop)
    )
    blocks.append("")
    blocks.extend(_render_recovery(phases))
    blocks.append("")
    blocks.extend(
        _render_refine(
            phases,
            sustained_goodput_rps=sustained_goodput_rps,
            sustained_users=sustained_users,
            stop_reason=stop_reason,
        )
    )
    return "\n".join(blocks).rstrip()

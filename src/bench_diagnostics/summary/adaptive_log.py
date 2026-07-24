"""Shared parsers for adaptive / explore-refine Locust controller logs."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches both legacy ``fail=N (pct%)`` and newer
# ``fail=N roll=…rps goodput=…/s fail%=…%`` stats snapshots.
ADAPTIVE_PHASE_RE = re.compile(
    r"adaptive phase end t=(\d+)s: (?P<action>.*?) \| "
    r"reqs=(?P<reqs>\d+) fail=(?P<fail>\d+)"
    r"(?:"
    r" \((?P<fail_pct_legacy>[^)]+)\)"
    r"|"
    r" roll=(?P<roll_rps>[\d.]+)rps goodput=(?P<roll_goodput>[\d.]+)/s "
    r"fail%=(?P<fail_pct_roll>[\d.]+)%"
    r")"
    r" p\d+=(?P<p95_logged>\S+)"
)

WARMUP_END_RE = re.compile(
    r"(?:adaptive-v2|explore-refine) warmup end t=(\d+)s at users=(\d+)"
    r"(?:.*?\|\s*)?"
    r"(?:reqs=(\d+) fail=(\d+)"
    r"(?: \((?P<fail_pct_legacy>[^)]+)\)|"
    r" roll=[\d.]+rps goodput=(?P<roll_goodput>[\d.]+)/s "
    r"fail%=(?P<fail_pct_roll>[\d.]+)%)?"
    r" p\d+=(?P<p95_logged>\S+))?"
)

ADAPTIVE_V2_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) "
    r"final_users=(?P<final_users>\S+) "
    r"low_ok=(?P<low_ok>\S+) "
    r"high_bad=(?P<high_bad>\S+) "
    r"goodput_history=\[(?P<history>[^\]]*)\]"
)

_EXPLORE_PEAK_RE = re.compile(r"peak=(?P<users>\d+)u/(?P<goodput>[\d.]+)/s")
_STEP_GOODPUT_RE = re.compile(r"step_goodput=([\d.]+)/s")
_USERS_IN_ACTION_RE = re.compile(r"users=(\d+)")


def phase_fail_pct(match: re.Match[str]) -> str | None:
    legacy = match.groupdict().get("fail_pct_legacy")
    if legacy:
        return str(legacy).rstrip("%")
    rolled = match.groupdict().get("fail_pct_roll")
    if rolled is not None:
        return str(rolled)
    return None


def phase_p95_token(match: re.Match[str]) -> str | None:
    raw = match.groupdict().get("p95_logged")
    return str(raw) if raw is not None else None


def phase_roll_goodput(match: re.Match[str]) -> float | None:
    raw = match.groupdict().get("roll_goodput")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_adaptive_stop(log_text: str) -> dict[str, str] | None:
    last: dict[str, str] | None = None
    for line in log_text.splitlines():
        m = ADAPTIVE_V2_STOP_RE.search(line)
        if m:
            last = {k: str(v) for k, v in m.groupdict().items() if v is not None}
    return last


def parse_explore_peak(log_text: str) -> tuple[int | None, float | None]:
    """Last explore-end peak users / goodput from recovery handoff lines."""
    users: int | None = None
    goodput: float | None = None
    for line in log_text.splitlines():
        if "explore end" not in line or "peak=" not in line:
            continue
        m = _EXPLORE_PEAK_RE.search(line)
        if not m:
            continue
        users = int(m.group("users"))
        goodput = float(m.group("goodput"))
    return users, goodput


@dataclass(frozen=True)
class AdaptiveRunOutcome:
    """Classified explore-refine (or adaptive-v2) bench outcome for feedback/plots."""

    kind: str
    title: str
    summary: str
    stop_reason: str | None = None
    explore_peak_users: int | None = None
    explore_peak_goodput_rps: float | None = None
    final_users: int | None = None
    final_goodput_rps: float | None = None
    p95_ms: float | None = None
    fail_pct: float | None = None
    refine_reached: bool = False
    premature: bool = False
    underestimate: bool = False
    sustained_goodput_rps: float | None = None

    def plot_box_text(self) -> str:
        """Multi-line label for the adaptive-ramp red outcome box."""
        lines = [self.title]
        for part in self.summary.split(". "):
            part = part.strip().rstrip(".")
            if part:
                lines.append(part + ("." if not part.endswith(")") else ""))
        # Keep the box compact: title + up to 3 detail lines.
        return "\n".join(lines[:4])

    def feedback_block(self) -> str:
        return "\n".join(
            [
                f"**Run outcome**: {self.title}",
                "",
                self.summary,
            ]
        )


def _as_float(token: str | None) -> float | None:
    if token is None:
        return None
    text = str(token).strip().rstrip("ms").rstrip("%")
    if text in ("", "n/a", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(token: str | None) -> int | None:
    if token is None or str(token) in ("None", "n/a", ""):
        return None
    try:
        return int(float(str(token)))
    except ValueError:
        return None


def _last_phase_match(log_text: str, *, action_substr: str) -> re.Match[str] | None:
    last: re.Match[str] | None = None
    for line in log_text.splitlines():
        if "adaptive phase end" not in line:
            continue
        m = ADAPTIVE_PHASE_RE.search(line)
        if not m:
            continue
        if action_substr in m.group("action"):
            last = m
    return last


def classify_adaptive_run_outcome(
    log_text: str,
    *,
    sustained_goodput_rps: float | None = None,
    sustained_users: int | None = None,
) -> AdaptiveRunOutcome | None:
    """
    Classify an explore-refine style bench stop into a user-facing outcome.

    Returns ``None`` when the log does not look like an adaptive controller run.
    """
    if not log_text.strip():
        return None
    adaptive = (
        "explore-refine" in log_text
        or "adaptive-v2 stop:" in log_text
        or "adaptive phase end" in log_text
    )
    if not adaptive:
        return None

    stop = parse_adaptive_stop(log_text)
    stop_reason = stop.get("reason") if stop else None
    final_users = _as_int(stop.get("final_users")) if stop else None
    explore_peak_users, explore_peak_gp = parse_explore_peak(log_text)
    refine_reached = (
        "recovery healthy" in log_text and "refine" in log_text
    ) or any(
        s in (stop_reason or "")
        for s in (
            "refine-goodput-stall",
            "overload-peak",
            "max-users-reached",
        )
    )
    # Timer during refine also counts as refine reached.
    if stop_reason == "run-time-elapsed" and "refine at users=" in log_text:
        refine_reached = True

    # --- Warmup failed ---
    if stop_reason == "warmup-unhealthy" or (
        "warmup unhealthy" in log_text and "stopping before explore" in log_text
    ):
        m = _last_phase_match(log_text, action_substr="warmup unhealthy")
        fail_pct = _as_float(phase_fail_pct(m)) if m else None
        p95 = _as_float(phase_p95_token(m)) if m else None
        goodput = phase_roll_goodput(m) if m else None
        bits = []
        if goodput is not None:
            bits.append(f"rolling goodput {goodput:.1f}/s")
        if fail_pct is not None:
            bits.append(f"fail% {fail_pct:.1f}")
        if p95 is not None:
            bits.append(f"p95 {p95:.0f}ms")
        if final_users is not None:
            bits.append(f"users={final_users}")
        detail = (
            "Application did not become healthy during warmup; explore never started."
            + (f" Last window: {', '.join(bits)}." if bits else "")
        )
        return AdaptiveRunOutcome(
            kind="warmup_unhealthy",
            title="Warmup unhealthy — explore not started",
            summary=detail,
            stop_reason=stop_reason or "warmup-unhealthy",
            final_users=final_users,
            final_goodput_rps=goodput,
            p95_ms=p95,
            fail_pct=fail_pct,
            refine_reached=False,
        )

    # --- Recovery failed ---
    if stop_reason == "recovery-unhealthy" or "recovery gave up" in log_text:
        m = _last_phase_match(log_text, action_substr="recovery unhealthy")
        last_gp = None
        last_users = final_users
        fail_pct = None
        p95 = None
        if m:
            last_gp = phase_roll_goodput(m)
            step_m = _STEP_GOODPUT_RE.search(m.string)
            if step_m:
                last_gp = float(step_m.group(1))
            users_m = _USERS_IN_ACTION_RE.search(m.group("action"))
            if users_m:
                last_users = int(users_m.group(1))
            fail_pct = _as_float(phase_fail_pct(m))
            p95 = _as_float(phase_p95_token(m))
        peak_bit = ""
        if explore_peak_gp is not None:
            peak_bit = (
                f"Reached explore peak goodput {explore_peak_gp:.1f}/s"
                + (
                    f" at {explore_peak_users} users. "
                    if explore_peak_users is not None
                    else ". "
                )
            )
        last_bit = ""
        if last_gp is not None or last_users is not None:
            if last_gp is not None:
                last_bit = (
                    f"Did not get healthy in recovery; last recovery level "
                    f"{last_gp:.1f}/s"
                )
            else:
                last_bit = "Did not get healthy in recovery; last recovery level"
            if last_users is not None:
                last_bit += f" at {last_users} users"
            last_bit += "."
            extras = []
            if fail_pct is not None:
                extras.append(f"fail% {fail_pct:.1f}")
            if p95 is not None:
                extras.append(f"p95 {p95:.0f}ms")
            if extras:
                last_bit += f" ({', '.join(extras)})."
        return AdaptiveRunOutcome(
            kind="recovery_unhealthy",
            title="Recovery unhealthy — refine not reached",
            summary=(peak_bit + last_bit).strip()
            or "Recovery failed; refine phase was never entered.",
            stop_reason=stop_reason or "recovery-unhealthy",
            explore_peak_users=explore_peak_users,
            explore_peak_goodput_rps=explore_peak_gp,
            final_users=last_users,
            final_goodput_rps=last_gp,
            p95_ms=p95,
            fail_pct=fail_pct,
            refine_reached=False,
        )

    # --- Refine outcomes ---
    if refine_reached:
        if stop_reason == "run-time-elapsed":
            gp = sustained_goodput_rps
            if gp is None:
                # Fall back to last refine step_goodput in the log if present.
                for line in reversed(log_text.splitlines()):
                    if "adaptive phase end" not in line:
                        continue
                    sm = _STEP_GOODPUT_RE.search(line)
                    if sm:
                        gp = float(sm.group(1))
                        break
            users = sustained_users if sustained_users is not None else final_users
            gp_bit = (
                f"Peak refine goodput observed ~{gp:.0f}/s"
                + (f" at {users} users" if users is not None else "")
                + " — probable underestimate (run timer expired before a plateau)."
                if gp is not None
                else "Run timer expired during refine before a stable plateau; reported goodput is likely an underestimate."
            )
            return AdaptiveRunOutcome(
                kind="refine_premature",
                title="Refine reached — run stopped prematurely (timer)",
                summary=gp_bit,
                stop_reason=stop_reason,
                explore_peak_users=explore_peak_users,
                explore_peak_goodput_rps=explore_peak_gp,
                final_users=users,
                final_goodput_rps=gp,
                refine_reached=True,
                premature=True,
                underestimate=True,
                sustained_goodput_rps=sustained_goodput_rps,
            )

        if stop_reason == "refine-goodput-stall":
            gp = sustained_goodput_rps
            detail = (
                f"Refine found a goodput plateau"
                + (
                    f" (best settled {gp:.0f}/s"
                    + (
                        f" at {sustained_users} users)."
                        if sustained_users is not None
                        else ")."
                    )
                    if gp is not None
                    else "."
                )
            )
            return AdaptiveRunOutcome(
                kind="refine_plateau",
                title="Refine complete — goodput plateau",
                summary=detail,
                stop_reason=stop_reason,
                explore_peak_users=explore_peak_users,
                explore_peak_goodput_rps=explore_peak_gp,
                final_users=sustained_users or final_users,
                final_goodput_rps=gp,
                refine_reached=True,
                sustained_goodput_rps=sustained_goodput_rps,
            )

        if stop_reason == "overload-peak":
            return AdaptiveRunOutcome(
                kind="refine_overload",
                title="Refine stopped — overload backoff limit",
                summary=(
                    "Refine repeatedly hit overload (p95/fail%) and stopped."
                    + (
                        f" Best settled refine goodput {sustained_goodput_rps:.0f}/s."
                        if sustained_goodput_rps is not None
                        else ""
                    )
                ),
                stop_reason=stop_reason,
                explore_peak_users=explore_peak_users,
                explore_peak_goodput_rps=explore_peak_gp,
                final_users=sustained_users or final_users,
                final_goodput_rps=sustained_goodput_rps,
                refine_reached=True,
                sustained_goodput_rps=sustained_goodput_rps,
            )

        if stop_reason == "max-users-reached":
            return AdaptiveRunOutcome(
                kind="refine_max_users",
                title="Refine stopped — max users reached",
                summary=(
                    "Refine hit the configured max users ceiling."
                    + (
                        f" Best settled refine goodput {sustained_goodput_rps:.0f}/s."
                        if sustained_goodput_rps is not None
                        else ""
                    )
                ),
                stop_reason=stop_reason,
                explore_peak_users=explore_peak_users,
                explore_peak_goodput_rps=explore_peak_gp,
                final_users=sustained_users or final_users,
                final_goodput_rps=sustained_goodput_rps,
                refine_reached=True,
                sustained_goodput_rps=sustained_goodput_rps,
            )

        # Refine ran but no scored settle peak / unknown stop.
        if sustained_goodput_rps is None:
            return AdaptiveRunOutcome(
                kind="refine_no_stable_window",
                title="Refine reached — no settled peak recorded",
                summary=(
                    "Refine started but no settled refine peak was recorded in the log."
                    + (f" Stop reason=`{stop_reason}`." if stop_reason else "")
                ),
                stop_reason=stop_reason,
                explore_peak_users=explore_peak_users,
                explore_peak_goodput_rps=explore_peak_gp,
                final_users=final_users,
                refine_reached=True,
            )

        return AdaptiveRunOutcome(
            kind="refine_complete",
            title="Refine reached — settled goodput recorded",
            summary=(
                f"Best settled refine goodput {sustained_goodput_rps:.0f}/s"
                + (
                    f" at {sustained_users} users."
                    if sustained_users is not None
                    else "."
                )
                + (f" Stop reason=`{stop_reason}`." if stop_reason else "")
            ),
            stop_reason=stop_reason,
            explore_peak_users=explore_peak_users,
            explore_peak_goodput_rps=explore_peak_gp,
            final_users=sustained_users or final_users,
            final_goodput_rps=sustained_goodput_rps,
            refine_reached=True,
            sustained_goodput_rps=sustained_goodput_rps,
        )

    # --- Timer / stop before refine ---
    if stop_reason == "run-time-elapsed":
        return AdaptiveRunOutcome(
            kind="timer_before_refine",
            title="Run timer expired before refine",
            summary=(
                "Wall-clock limit hit during explore/recovery; refine never scored."
                + (
                    f" Explore peak was {explore_peak_gp:.1f}/s"
                    + (
                        f" at {explore_peak_users} users."
                        if explore_peak_users is not None
                        else "."
                    )
                    if explore_peak_gp is not None
                    else ""
                )
            ),
            stop_reason=stop_reason,
            explore_peak_users=explore_peak_users,
            explore_peak_goodput_rps=explore_peak_gp,
            final_users=final_users,
            refine_reached=False,
            premature=True,
        )

    if stop_reason:
        return AdaptiveRunOutcome(
            kind="other",
            title=f"Run stopped — {stop_reason}",
            summary=f"Controller stop reason=`{stop_reason}`.",
            stop_reason=stop_reason,
            explore_peak_users=explore_peak_users,
            explore_peak_goodput_rps=explore_peak_gp,
            final_users=final_users,
            refine_reached=refine_reached,
            sustained_goodput_rps=sustained_goodput_rps,
        )

    return None

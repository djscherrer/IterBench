from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

_WARMUP_END_RE = re.compile(
    r"adaptive-v2 warmup end t=(\d+)s at users=(\d+)"
)
_ADAPTIVE_PHASE_RE = re.compile(
    r"adaptive phase end t=(\d+)s: (?P<action>.*?) \| "
    r"reqs=(?P<reqs>\d+) fail=(?P<fail>\d+) \((?P<fail_pct>[^)]+)\) "
    r"p\d+=(?P<p95_logged>\S+)"
)
_P95_IN_ACTION_RE = re.compile(r"p95=(\d+)ms")
_FAIL_PCT_IN_ACTION_RE = re.compile(r"fail%=([\d.]+)")
_NEXT_USERS_RE = re.compile(r"users=(\d+)")
_STEP_USERS_RE = re.compile(r"step=(\d+)")
_STEP_GOODPUT_RE = re.compile(r"step_goodput=([\d.]+)/s")
_STEP_REQS_RE = re.compile(r"step_reqs=(\d+)")
_STEP_FAIL_RE = re.compile(r"step_fail=(\d+)")
_SAMPLES_RE = re.compile(r"samples=\[(?P<samples>[^\]]*)\]")
_P95_SAMPLE_RE = re.compile(
    r"adaptive p95 sample t=(\d+)s users=(\d+) p95=([\d.]+)ms"
)
_GOODPUT_SAMPLE_RE = re.compile(
    r"adaptive goodput sample t=(\d+)s users=(\d+) goodput=([\d.]+)/s"
)
_GOODPUT_HISTORY_ENTRY_RE = re.compile(r"(\d+)u:([\d.]+)/s")
_ADAPTIVE_V2_STOP_RE = re.compile(
    r"adaptive-v2 stop: reason=(?P<reason>\S+) "
    r"final_users=(?P<final_users>\S+) "
    r"low_ok=(?P<low_ok>\S+) "
    r"high_bad=(?P<high_bad>\S+) "
    r"goodput_history=\[(?P<history>[^\]]*)\]"
)


@dataclass(frozen=True)
class PeakGoodputMarker:
    """Run-level goodput marker for adaptive ramp / trajectory plots."""

    t_s: int
    goodput_rps: float
    users: int | None


@dataclass(frozen=True)
class SustainedGoodputResult:
    """Max eligible rolling-window goodput from Locust ``stats_history``."""

    goodput_rps: float
    users: int | None
    t_s: int
    window_s: int
    fail_pct: float
    drift_pct: float


# k8s-adaptive-v2-new defaults (see registry.py)
_DEFAULT_V2_TRIM_S = 10
_DEFAULT_V2_MIN_STEP_S = 15
_DEFAULT_V2_SAMPLE_EVERY_S = 1
_DEFAULT_V2_MIN_SETTLE_SAMPLES = 5
_DEFAULT_V2_SLA_MS = 300.0
_DEFAULT_SUSTAINED_WINDOW_S = 10
_DEFAULT_FAILURE_THRESHOLD_PCT = 2.0
_DEFAULT_STABILITY_DRIFT_PCT = 5.0


@dataclass(frozen=True)
class AdaptivePlotParams:
    """Controller timing params for P95 timeline / decision-window plotting."""

    trim_s: int
    sample_every_s: int
    min_settle_samples: int
    sla_ms: float


@dataclass(frozen=True)
class SustainedGoodputParams:
    """Thresholds for sustained-goodput scoring from ``stats_history``."""

    window_s: int
    failure_threshold_pct: float
    stability_drift_threshold_pct: float


def load_adaptive_plot_params(bench_dir: Path) -> AdaptivePlotParams:
    """
    Resolve adaptive plot params from ``05-bench/config.json`` → load profile.

    Falls back to registry defaults when config or profile is missing.
    """
    defaults = AdaptivePlotParams(
        trim_s=_DEFAULT_V2_TRIM_S,
        sample_every_s=_DEFAULT_V2_SAMPLE_EVERY_S,
        min_settle_samples=_DEFAULT_V2_MIN_SETTLE_SAMPLES,
        sla_ms=_DEFAULT_V2_SLA_MS,
    )
    config_path = bench_dir / "config.json"
    if not config_path.is_file():
        return defaults
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults

    from bench_diagnostics.summary.load_run import load_profile_from_config
    from locust_bench.load_profiles.manifest import resolved_profile_from_bench_config
    from locust_bench.load_profiles.models import (
        AdaptiveLoadProfile,
        AdaptiveV2LoadProfile,
        GoodputPlateauLoadProfile,
    )
    from locust_bench.load_profiles.registry import resolve_load_profile

    resolved = resolved_profile_from_bench_config(config)
    if resolved is not None:
        mode = str(resolved.get("mode") or "")
        if mode in ("adaptive_v2", "goodput_plateau", "adaptive"):
            settle = resolved.get("min_settle_samples", resolved.get("settle_samples"))
            return AdaptivePlotParams(
                trim_s=int(resolved["trim_s"]),
                sample_every_s=int(resolved["sample_every_s"]),
                min_settle_samples=int(settle),
                sla_ms=float(resolved.get("sla_ms", _DEFAULT_V2_SLA_MS)),
            )

    name = load_profile_from_config(config)
    if not name:
        return defaults
    try:
        profile = resolve_load_profile(name)
    except KeyError:
        return defaults

    if isinstance(profile, AdaptiveV2LoadProfile):
        return AdaptivePlotParams(
            trim_s=int(profile.trim_s),
            sample_every_s=int(profile.sample_every_s),
            min_settle_samples=int(profile.min_settle_samples),
            sla_ms=float(profile.sla_ms),
        )
    if isinstance(profile, GoodputPlateauLoadProfile):
        return AdaptivePlotParams(
            trim_s=int(profile.trim_s),
            sample_every_s=int(profile.sample_every_s),
            min_settle_samples=int(profile.min_settle_samples),
            sla_ms=_DEFAULT_V2_SLA_MS,
        )
    if isinstance(profile, AdaptiveLoadProfile):
        return AdaptivePlotParams(
            trim_s=int(profile.trim_s),
            sample_every_s=int(profile.sample_every_s),
            min_settle_samples=int(profile.settle_samples),
            sla_ms=float(profile.sla_ms),
        )
    return defaults


def load_sustained_goodput_params(bench_dir: Path) -> SustainedGoodputParams:
    """Resolve sustained-goodput scoring params from ``05-bench/config.json``."""
    defaults = SustainedGoodputParams(
        window_s=_DEFAULT_SUSTAINED_WINDOW_S,
        failure_threshold_pct=_DEFAULT_FAILURE_THRESHOLD_PCT,
        stability_drift_threshold_pct=_DEFAULT_STABILITY_DRIFT_PCT,
    )
    config_path = bench_dir / "config.json"
    if not config_path.is_file():
        return defaults
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults

    from bench_diagnostics.summary.load_run import load_profile_from_config
    from locust_bench.load_profiles.manifest import resolved_profile_from_bench_config
    from locust_bench.load_profiles.models import AdaptiveV2LoadProfile, GoodputPlateauLoadProfile
    from locust_bench.load_profiles.registry import resolve_load_profile

    resolved = resolved_profile_from_bench_config(config)
    if resolved is not None and str(resolved.get("mode") or "") in (
        "goodput_plateau",
        "adaptive_v2",
    ):
        return SustainedGoodputParams(
            window_s=_DEFAULT_SUSTAINED_WINDOW_S,
            failure_threshold_pct=float(resolved["failure_threshold_pct"]),
            stability_drift_threshold_pct=float(resolved["stability_drift_threshold_pct"]),
        )

    name = load_profile_from_config(config)
    if not name:
        return defaults
    try:
        profile = resolve_load_profile(name)
    except KeyError:
        return defaults

    if isinstance(profile, (GoodputPlateauLoadProfile, AdaptiveV2LoadProfile)):
        return SustainedGoodputParams(
            window_s=_DEFAULT_SUSTAINED_WINDOW_S,
            failure_threshold_pct=float(profile.failure_threshold_pct),
            stability_drift_threshold_pct=float(profile.stability_drift_threshold_pct),
        )
    return defaults


def sustained_goodput_from_timeseries(
    df: pd.DataFrame,
    *,
    window_s: int = _DEFAULT_SUSTAINED_WINDOW_S,
    failure_threshold_pct: float = _DEFAULT_FAILURE_THRESHOLD_PCT,
    stability_drift_threshold_pct: float = _DEFAULT_STABILITY_DRIFT_PCT,
) -> SustainedGoodputResult | None:
    """
    Find the highest sustained goodput over a rolling window.

    A window is eligible when:
    - virtual users are flat across the window
    - window failure rate is at most ``failure_threshold_pct``
    - (max − min) goodput / mean goodput in the window is at most
      ``stability_drift_threshold_pct`` percent
    """
    if df.empty:
        return None

    w = max(1, int(window_s))
    if len(df) < w:
        return None

    best: SustainedGoodputResult | None = None
    for end_idx in range(w - 1, len(df)):
        chunk = df.iloc[end_idx - w + 1 : end_idx + 1]
        users = chunk["users"]
        if users.isna().any() or float(users.max()) != float(users.min()):
            continue

        total_req = float(chunk["req_rps"].sum())
        if total_req <= 0.0:
            continue
        total_fail = float(chunk["fail_rps"].sum())
        fail_pct = 100.0 * total_fail / total_req
        if fail_pct > float(failure_threshold_pct):
            continue

        goodput = chunk["goodput_rps"].astype(float)
        gp_mean = float(goodput.mean())
        if gp_mean <= 0.0:
            continue
        gp_min = float(goodput.min())
        gp_max = float(goodput.max())
        drift_pct = 100.0 * (gp_max - gp_min) / gp_mean
        if drift_pct > float(stability_drift_threshold_pct):
            continue

        candidate = SustainedGoodputResult(
            goodput_rps=gp_mean,
            users=int(round(float(users.iloc[-1]))),
            t_s=int(round(float(chunk["t_s"].iloc[-1]))),
            window_s=w,
            fail_pct=fail_pct,
            drift_pct=drift_pct,
        )
        if best is None or candidate.goodput_rps > best.goodput_rps:
            best = candidate
    return best


def sustained_goodput_from_bench(
    bench_dir: Path,
    *,
    params: SustainedGoodputParams | None = None,
) -> SustainedGoodputResult | None:
    """Sustained max goodput for one ``05-bench/`` directory."""
    scoring = params or load_sustained_goodput_params(bench_dir)
    try:
        df = load_stats_timeseries(bench_dir)
    except (FileNotFoundError, ValueError):
        return None
    return sustained_goodput_from_timeseries(
        df,
        window_s=scoring.window_s,
        failure_threshold_pct=scoring.failure_threshold_pct,
        stability_drift_threshold_pct=scoring.stability_drift_threshold_pct,
    )


@dataclass(frozen=True)
class AdaptiveDecision:
    t_s: int
    p95_ms: float | None
    fail_pct: float | None
    users_at_step: int | None
    users_after: int | None
    user_delta: int | None
    label: str
    changes_users: bool
    step_goodput_rps: float | None = None
    step_reqs: int | None = None
    step_samples: tuple[float, ...] = ()


@dataclass(frozen=True)
class LatencySample:
    """One windowed P95 reading from the adaptive controller."""

    t_s: int
    p95_ms: float
    users: int | None = None


@dataclass(frozen=True)
class P95Timeline:
    """Controller P95 samples for plotting."""

    all_samples: tuple[LatencySample, ...]
    decision_samples: tuple[LatencySample, ...]
    has_full_timeline: bool


@dataclass(frozen=True)
class GoodputSample:
    """One goodput (successful req/s) reading from the controller."""

    t_s: int
    goodput_rps: float
    users: int | None = None


@dataclass(frozen=True)
class GoodputTimeline:
    """Controller goodput samples for plotting."""

    all_samples: tuple[GoodputSample, ...]
    decision_samples: tuple[GoodputSample, ...]
    has_full_timeline: bool


def gather_bench_log_text(bench_dir: Path) -> str:
    """Merge ``bench.log`` and ``locust/logs/**`` (adaptive lines may live in either)."""
    chunks: list[str] = []
    bench_log = bench_dir / "bench.log"
    if bench_log.is_file():
        chunks.append(bench_log.read_text(encoding="utf-8", errors="replace"))
    for sub in ("locust/logs", "logs"):
        logs_root = bench_dir / sub
        if not logs_root.is_dir():
            continue
        for path in sorted(logs_root.rglob("*.log")):
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _parse_samples_list(line: str) -> tuple[float, ...]:
    m = _SAMPLES_RE.search(line)
    if not m:
        return ()
    values: list[float] = []
    for part in m.group("samples").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            continue
    return tuple(values)


def _parse_samples_mean(line: str) -> float | None:
    values = _parse_samples_list(line)
    if not values:
        return None
    return float(statistics.mean(values))


def _parse_p95_ms(action: str, p95_logged: str, line: str) -> float | None:
    """
    P95 that drove the decision at this step.

    1. Ramp steps embed it in the action (``below SLA (p95=58ms…)``).
    2. Bracket steps omit it; use ``mean(samples)`` (controller input).
    3. Never use ``p95_logged`` here — that is cumulative Locust aggregate p95.
    """
    m = _P95_IN_ACTION_RE.search(action)
    if m:
        return float(m.group(1))
    return _parse_samples_mean(line)


def _parse_step_fail_pct(line: str, action: str) -> float | None:
    m = _FAIL_PCT_IN_ACTION_RE.search(action)
    if m:
        return float(m.group(1))
    step_reqs_m = _STEP_REQS_RE.search(line)
    step_fail_m = _STEP_FAIL_RE.search(line)
    if step_reqs_m and step_fail_m:
        step_reqs = int(step_reqs_m.group(1))
        step_fail = int(step_fail_m.group(1))
        if step_reqs > 0:
            return 100.0 * step_fail / step_reqs
    return None


def _build_short_label(
    *,
    action: str,
    p95_ms: float | None,
    fail_pct: float | None,
    users_after: int | None,
    user_delta: int | None,
    changes_users: bool,
) -> str:
    parts: list[str] = []
    if p95_ms is not None:
        parts.append(f"P95={p95_ms:.0f}ms")
    if fail_pct is not None:
        parts.append(f"err={fail_pct:.1f}%")
    if not changes_users:
        if "stop" in action.lower() or "stopping" in action.lower():
            parts.append("stop")
        return ", ".join(parts) if parts else "hold"
    if user_delta is not None and user_delta != 0:
        sign = "+" if user_delta > 0 else "−"
        parts.append(f"→ {sign}{abs(user_delta)} users")
    elif users_after is not None:
        parts.append(f"→ {users_after} users")
    return ", ".join(parts) if parts else "ramp"


def bench_log_needs_goodput_correction(log_text: str) -> bool:
    """
    Whether logged ``step_goodput`` values need legacy inflation correction.

    Runs that log ``drift=`` use the fixed shape formula (full step elapsed);
    older ``cv=`` logs inflated goodput by ``elapsed / (elapsed - trim)``.
    """
    return " drift=" not in log_text


def correct_step_goodput_rps(
    goodput_rps: float | None,
    *,
    step_reqs: int | None = None,
    trim_s: int = _DEFAULT_V2_TRIM_S,
) -> float | None:
    """
    Correct legacy step goodput inflated by ``elapsed/(elapsed-trim)``.

    Before the shape fix, ``delta_reqs`` covered the full step but the
    denominator used ``elapsed - trim_s`` only.
    """
    if goodput_rps is None or goodput_rps <= 0:
        return goodput_rps
    if step_reqs is None or step_reqs <= 0:
        return goodput_rps * (_DEFAULT_V2_MIN_STEP_S - trim_s) / _DEFAULT_V2_MIN_STEP_S
    measured_s = step_reqs / goodput_rps
    if measured_s <= 0:
        return goodput_rps
    elapsed_s = measured_s + float(trim_s)
    return step_reqs / elapsed_s


def format_decision_tuple(decision: AdaptiveDecision) -> tuple[str, str, str]:
    """
    Compact three-line decision box.

    Line 1: ``(P95 ms, err%)``
    Line 2: virtual users at this step (``@643u``)
    Line 3: user change (``+200``, ``-94``, ``stop``, …)
    """
    p95 = f"{decision.p95_ms:.0f}ms" if decision.p95_ms is not None else "—"
    err = f"{decision.fail_pct:.1f}%" if decision.fail_pct is not None else "—"
    line1 = f"({p95}, {err})"
    users = decision.users_at_step
    line2 = f"@{users}u" if users is not None else "—"
    line3 = _format_user_delta_short(decision)
    return line1, line2, line3


def _format_user_delta_short(decision: AdaptiveDecision) -> str:
    if "stop" in decision.label.lower():
        return "stop"
    if decision.user_delta is not None and decision.user_delta != 0:
        return f"{decision.user_delta:+d}"
    if decision.users_after is not None:
        return f"→{decision.users_after}"
    return "—"


def _parse_goodput_history(text: str) -> list[tuple[int, float]]:
    return [
        (int(m.group(1)), float(m.group(2)))
        for m in _GOODPUT_HISTORY_ENTRY_RE.finditer(text)
    ]


def _decision_matches_users(
    decision: AdaptiveDecision,
    users: int,
) -> bool:
    return users in {decision.users_at_step, decision.users_after}


def _adjusted_step_goodput(
    goodput_rps: float | None,
    *,
    step_reqs: int | None,
    needs_correction: bool,
) -> float:
    if goodput_rps is None or goodput_rps <= 0:
        return 0.0
    if not needs_correction:
        return goodput_rps
    return correct_step_goodput_rps(goodput_rps, step_reqs=step_reqs) or goodput_rps


def peak_goodput_from_bench_log(
    log_text: str,
    history: list[tuple[int, float]] | None = None,
) -> tuple[float, int | None]:
    """
    Run peak goodput (successful req/s) for experiment trajectory plots.

    Uses the max ``step_goodput`` across all adaptive steps (includes bracket
    refinements not always present in ``goodput_history``). Applies legacy
    inflation correction only for pre-``drift=`` logs.
    """
    needs_correction = bench_log_needs_goodput_correction(log_text)
    decisions = parse_adaptive_decisions(log_text)
    candidates = [d for d in decisions if d.step_goodput_rps is not None]
    if candidates:
        best = max(
            candidates,
            key=lambda d: _adjusted_step_goodput(
                d.step_goodput_rps,
                step_reqs=d.step_reqs,
                needs_correction=needs_correction,
            ),
        )
        peak_rps = _adjusted_step_goodput(
            best.step_goodput_rps,
            step_reqs=best.step_reqs,
            needs_correction=needs_correction,
        )
        return peak_rps, best.users_at_step or best.users_after

    hist = history if history is not None else []
    if not hist:
        for line in log_text.splitlines():
            m = _ADAPTIVE_V2_STOP_RE.search(line)
            if m:
                hist = _parse_goodput_history(m.group("history"))
    if hist:
        peak_users, peak_rps_raw = max(hist, key=lambda item: item[1])
        matched = [
            d for d in decisions if _decision_matches_users(d, peak_users)
        ]
        step_reqs = matched[-1].step_reqs if matched else None
        peak_rps = _adjusted_step_goodput(
            peak_rps_raw,
            step_reqs=step_reqs,
            needs_correction=needs_correction,
        )
        return peak_rps, peak_users

    values = [float(m.group(1)) for m in _STEP_GOODPUT_RE.finditer(log_text)]
    if values:
        peak_rps_raw = max(values)
        peak_rps = _adjusted_step_goodput(
            peak_rps_raw,
            step_reqs=None,
            needs_correction=needs_correction,
        )
        return peak_rps, None
    return 0.0, None


def resolve_run_goodput_marker(
    bench_dir: Path,
    log_text: str,
    decisions: list[AdaptiveDecision],
) -> PeakGoodputMarker | None:
    """
    Primary run goodput marker for plots and experiment summaries.

    Uses sustained max goodput from ``stats_history`` when available; falls back
    to the legacy per-step peak metric otherwise.
    """
    sustained = sustained_goodput_from_bench(bench_dir)
    if sustained is not None and sustained.goodput_rps > 0:
        return PeakGoodputMarker(
            t_s=sustained.t_s,
            goodput_rps=sustained.goodput_rps,
            users=sustained.users,
        )
    return resolve_peak_goodput_marker(log_text, decisions)


def resolve_peak_goodput_marker(
    log_text: str,
    decisions: list[AdaptiveDecision],
) -> PeakGoodputMarker | None:
    """Legacy peak goodput from controller step metrics."""
    needs_correction = bench_log_needs_goodput_correction(log_text)
    candidates = [d for d in decisions if d.step_goodput_rps is not None]
    if candidates:
        best = max(
            candidates,
            key=lambda d: _adjusted_step_goodput(
                d.step_goodput_rps,
                step_reqs=d.step_reqs,
                needs_correction=needs_correction,
            ),
        )
        return PeakGoodputMarker(
            t_s=best.t_s,
            goodput_rps=_adjusted_step_goodput(
                best.step_goodput_rps,
                step_reqs=best.step_reqs,
                needs_correction=needs_correction,
            ),
            users=best.users_at_step or best.users_after,
        )

    peak_rps, peak_users = peak_goodput_from_bench_log(log_text)
    if peak_rps <= 0:
        return None
    matched = [
        d for d in decisions if peak_users is not None and _decision_matches_users(d, peak_users)
    ]
    return PeakGoodputMarker(
        t_s=matched[-1].t_s if matched else (decisions[-1].t_s if decisions else 0),
        goodput_rps=peak_rps,
        users=peak_users,
    )


def plottable_decisions(decisions: list[AdaptiveDecision]) -> list[AdaptiveDecision]:
    """Decisions to list in the left panel (user changes + final stop)."""
    result: list[AdaptiveDecision] = []
    for decision in decisions:
        if decision.label == "warmup end":
            continue
        if decision.changes_users or "stop" in decision.label.lower():
            result.append(decision)
    return result


def parse_adaptive_decisions(log_text: str) -> list[AdaptiveDecision]:
    """Parse adaptive controller decision points from bench / Locust logs."""
    seen_t: set[int] = set()
    decisions: list[AdaptiveDecision] = []
    prev_users: int | None = None

    for line in log_text.splitlines():
        warmup_m = _WARMUP_END_RE.search(line)
        if warmup_m:
            t_s = int(warmup_m.group(1))
            users = int(warmup_m.group(2))
            if t_s not in seen_t:
                seen_t.add(t_s)
                decisions.append(
                    AdaptiveDecision(
                        t_s=t_s,
                        p95_ms=None,
                        fail_pct=None,
                        users_at_step=users,
                        users_after=users,
                        user_delta=None,
                        label="warmup end",
                        changes_users=False,
                    )
                )
            prev_users = users
            continue

        phase_m = _ADAPTIVE_PHASE_RE.search(line)
        if not phase_m:
            continue
        t_s = int(phase_m.group(1))
        if t_s in seen_t:
            continue
        seen_t.add(t_s)

        action = phase_m.group("action").strip()
        p95_ms = _parse_p95_ms(action, phase_m.group("p95_logged"), line)
        fail_pct = _parse_step_fail_pct(line, action)

        users_after_m = _NEXT_USERS_RE.search(action)
        users_after = int(users_after_m.group(1)) if users_after_m else None
        step_m = _STEP_USERS_RE.search(action)
        step_users = int(step_m.group(1)) if step_m else None

        changes_users = False
        user_delta: int | None = None
        if "stopping" in action.lower() or "bracket narrow" in action.lower():
            changes_users = False
        elif users_after is not None and prev_users is not None:
            user_delta = users_after - prev_users
            changes_users = user_delta != 0
        elif step_users is not None:
            if "backoff" in action.lower():
                user_delta = -step_users
            else:
                user_delta = step_users
            changes_users = user_delta != 0

        goodput_m = _STEP_GOODPUT_RE.search(line)
        step_goodput = float(goodput_m.group(1)) if goodput_m else None
        step_reqs_m = _STEP_REQS_RE.search(line)
        step_reqs = int(step_reqs_m.group(1)) if step_reqs_m else None
        users_at_step = prev_users
        step_samples = _parse_samples_list(line)

        label = _build_short_label(
            action=action,
            p95_ms=p95_ms,
            fail_pct=fail_pct,
            users_after=users_after,
            user_delta=user_delta,
            changes_users=changes_users,
        )
        decisions.append(
            AdaptiveDecision(
                t_s=t_s,
                p95_ms=p95_ms,
                fail_pct=fail_pct,
                users_at_step=users_at_step,
                users_after=users_after,
                user_delta=user_delta,
                label=label,
                changes_users=changes_users,
                step_goodput_rps=step_goodput,
                step_reqs=step_reqs,
                step_samples=step_samples,
            )
        )
        if users_after is not None:
            prev_users = users_after
        elif users_at_step is not None:
            prev_users = users_at_step

    decisions.sort(key=lambda d: d.t_s)
    return decisions


def load_stats_timeseries(bench_dir: Path) -> pd.DataFrame:
    """Load aggregated Locust stats history as a per-second time series."""
    stats_candidates = sorted(
        (bench_dir / "locust" / "results").glob("*_stats_history.csv")
    )
    if not stats_candidates:
        raise FileNotFoundError(
            f"No locust stats_history CSV in {bench_dir / 'locust' / 'results'}"
        )
    df = pd.read_csv(stats_candidates[0])
    df = df[df["Name"] == "Aggregated"].copy()
    if df.empty:
        raise ValueError(f"No Aggregated rows in {stats_candidates[0]}")

    for col in ("Timestamp", "User Count", "Requests/s", "Failures/s", "95%"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Timestamp", "Requests/s", "Failures/s"])
    if df.empty:
        raise ValueError("Stats history empty after numeric coercion")

    df = df.sort_values("Timestamp").reset_index(drop=True)
    df["t_s"] = df["Timestamp"] - df["Timestamp"].min()
    df["goodput_rps"] = df["Requests/s"] - df["Failures/s"]
    df["req_rps"] = df["Requests/s"]
    df["fail_rps"] = df["Failures/s"]
    if "User Count" in df.columns:
        df["users"] = df["User Count"].ffill().fillna(0)
    else:
        df["users"] = 0.0
    if "95%" in df.columns:
        df["p95_ms"] = pd.to_numeric(df["95%"], errors="coerce")
    else:
        df["p95_ms"] = float("nan")
    return df


def smooth_series(series: pd.Series, window_s: int) -> pd.Series:
    """Rolling mean over ``window_s`` samples (Locust emits ~1 sample/s)."""
    w = max(1, int(window_s))
    return series.rolling(window=w, min_periods=1).mean()


def parse_controller_p95_timeline(
    log_text: str,
    decisions: list[AdaptiveDecision],
    *,
    trim_s: int = _DEFAULT_V2_TRIM_S,
    sample_every_s: int = _DEFAULT_V2_SAMPLE_EVERY_S,
    min_settle_samples: int = _DEFAULT_V2_MIN_SETTLE_SAMPLES,
) -> P95Timeline:
    """
    Build the controller P95 timeline for plotting.

    When per-second ``adaptive p95 sample`` lines exist, ``all_samples`` holds
    every reading and ``decision_samples`` marks the last ``min_settle_samples``
    before each step decision (the median window).

    Legacy runs only log the decision window (``samples=[…]`` at phase end);
    then ``all_samples == decision_samples`` and ``has_full_timeline`` is False.
    """
    explicit = _parse_explicit_p95_samples(log_text)
    if explicit:
        decision_samples = _decision_window_samples(
            explicit,
            decisions,
            trim_s=trim_s,
            min_settle_samples=min_settle_samples,
        )
        return P95Timeline(
            all_samples=tuple(explicit),
            decision_samples=tuple(decision_samples),
            has_full_timeline=True,
        )

    decision_samples = reconstruct_latency_samples_from_decisions(
        decisions,
        trim_s=trim_s,
        sample_every_s=sample_every_s,
    )
    return P95Timeline(
        all_samples=tuple(decision_samples),
        decision_samples=tuple(decision_samples),
        has_full_timeline=False,
    )


def parse_controller_goodput_timeline(
    log_text: str,
    decisions: list[AdaptiveDecision],
    *,
    trim_s: int = _DEFAULT_V2_TRIM_S,
    min_settle_samples: int = _DEFAULT_V2_MIN_SETTLE_SAMPLES,
) -> GoodputTimeline:
    """
    Build the controller goodput timeline for plotting.

    Uses explicit ``adaptive goodput sample`` lines when present.
    ``decision_samples`` marks the last ``min_settle_samples`` readings before
    each step decision (mirrors P95 decision-window selection).
    """
    all_samples: list[GoodputSample] = []
    for line in log_text.splitlines():
        m = _GOODPUT_SAMPLE_RE.search(line)
        if not m:
            continue
        all_samples.append(
            GoodputSample(
                t_s=int(m.group(1)),
                users=int(m.group(2)),
                goodput_rps=float(m.group(3)),
            )
        )
    all_samples.sort(key=lambda s: s.t_s)
    if not all_samples:
        return GoodputTimeline(all_samples=(), decision_samples=(), has_full_timeline=False)

    decision_samples = _decision_window_goodput_samples(
        all_samples,
        decisions,
        trim_s=trim_s,
        min_settle_samples=min_settle_samples,
    )
    return GoodputTimeline(
        all_samples=tuple(all_samples),
        decision_samples=tuple(decision_samples),
        has_full_timeline=True,
    )


def _decision_window_goodput_samples(
    all_samples: list[GoodputSample],
    decisions: list[AdaptiveDecision],
    *,
    trim_s: int,
    min_settle_samples: int,
) -> list[GoodputSample]:
    warmup = next((d for d in decisions if d.label == "warmup end"), None)
    phases = [d for d in decisions if d.label != "warmup end"]
    if not phases:
        return []
    relevant: list[GoodputSample] = []
    for i, decision in enumerate(phases):
        level_start = warmup.t_s if warmup and i == 0 else phases[i - 1].t_s
        first_sample_t = int(round(level_start + trim_s))
        level_samples = [s for s in all_samples if first_sample_t <= s.t_s <= decision.t_s]
        if not level_samples:
            continue
        relevant.extend(level_samples[-min_settle_samples:])
    return relevant


def parse_controller_p95_samples(
    log_text: str,
    decisions: list[AdaptiveDecision],
    *,
    trim_s: int = _DEFAULT_V2_TRIM_S,
    sample_every_s: int = _DEFAULT_V2_SAMPLE_EVERY_S,
) -> list[LatencySample]:
    """All controller samples (see :func:`parse_controller_p95_timeline`)."""
    return list(
        parse_controller_p95_timeline(
            log_text,
            decisions,
            trim_s=trim_s,
            sample_every_s=sample_every_s,
        ).all_samples
    )


def _parse_explicit_p95_samples(log_text: str) -> list[LatencySample]:
    explicit: list[LatencySample] = []
    for line in log_text.splitlines():
        m = _P95_SAMPLE_RE.search(line)
        if not m:
            continue
        explicit.append(
            LatencySample(
                t_s=int(m.group(1)),
                users=int(m.group(2)),
                p95_ms=float(m.group(3)),
            )
        )
    explicit.sort(key=lambda s: s.t_s)
    return explicit


def _phase_decisions(decisions: list[AdaptiveDecision]) -> list[AdaptiveDecision]:
    return [d for d in decisions if d.label != "warmup end"]


def _decision_window_samples(
    all_samples: list[LatencySample],
    decisions: list[AdaptiveDecision],
    *,
    trim_s: int,
    min_settle_samples: int,
) -> list[LatencySample]:
    """Last ``min_settle_samples`` readings before each step decision."""
    warmup = next((d for d in decisions if d.label == "warmup end"), None)
    phases = _phase_decisions(decisions)
    if not phases:
        return []

    relevant: list[LatencySample] = []
    for i, decision in enumerate(phases):
        level_start = warmup.t_s if warmup and i == 0 else phases[i - 1].t_s
        first_sample_t = int(round(level_start + trim_s))
        level_samples = [
            s
            for s in all_samples
            if first_sample_t <= s.t_s <= decision.t_s
        ]
        if not level_samples:
            continue
        window = level_samples[-min_settle_samples:]
        relevant.extend(window)
    return relevant


def reconstruct_latency_samples_from_decisions(
    decisions: list[AdaptiveDecision],
    *,
    trim_s: int = _DEFAULT_V2_TRIM_S,
    sample_every_s: int = _DEFAULT_V2_SAMPLE_EVERY_S,
) -> list[LatencySample]:
    """
    Expand each step's logged ``samples=[…]`` onto a timeline.

    The log only retains the last ``min_settle_samples`` values used for the
    step median. Align them to end at the decision second (not level_start+trim).
    """
    points: list[LatencySample] = []
    level_start: int | None = None
    for decision in sorted(decisions, key=lambda d: d.t_s):
        if decision.label == "warmup end":
            level_start = decision.t_s
            continue
        if level_start is None or not decision.step_samples:
            level_start = decision.t_s
            continue
        n = len(decision.step_samples)
        first_sample_t = int(round(level_start + trim_s))
        for i, value in enumerate(decision.step_samples):
            t_sample = decision.t_s - (n - 1 - i) * sample_every_s
            t_sample = max(first_sample_t, int(round(t_sample)))
            if t_sample <= decision.t_s:
                points.append(
                    LatencySample(
                        t_s=t_sample,
                        p95_ms=value,
                        users=decision.users_at_step,
                    )
                )
        level_start = decision.t_s
    return points


def group_latency_sample_segments(
    samples: list[LatencySample],
    *,
    max_gap_s: int = 2,
) -> list[list[LatencySample]]:
    """Split samples into contiguous segments (breaks across trim gaps)."""
    if not samples:
        return []
    ordered = sorted(samples, key=lambda s: s.t_s)
    segments: list[list[LatencySample]] = [[ordered[0]]]
    for sample in ordered[1:]:
        if sample.t_s - segments[-1][-1].t_s > max_gap_s:
            segments.append([sample])
        else:
            segments[-1].append(sample)
    return segments


def build_controller_p95_continuous_series(
    t_s: pd.Series,
    samples: list[LatencySample],
) -> pd.Series:
    """Forward-fill controller samples onto the stats-history time grid."""
    if not samples:
        return pd.Series(float("nan"), index=t_s.index)
    ordered = sorted(samples, key=lambda s: s.t_s)
    values: list[float] = []
    idx = 0
    current = float("nan")
    for t in t_s:
        while idx < len(ordered) and ordered[idx].t_s <= t:
            current = ordered[idx].p95_ms
            idx += 1
        values.append(current)
    return pd.Series(values, index=t_s.index)


def build_controller_p95_step_series(
    t_s: pd.Series,
    decisions: list[AdaptiveDecision],
) -> pd.Series:
    """
    Step-held P95 from the adaptive controller (median of windowed samples).

    Each decision records the per-step latency that drove the ramp; values
    are held from that decision time until the next one. This matches the
    decision boxes and bench logs, unlike Locust ``stats_history`` ``95%``
    which is cumulative over the whole run.
    """
    points = sorted(
        (d.t_s, d.p95_ms)
        for d in decisions
        if d.p95_ms is not None and d.label != "warmup end"
    )
    if not points:
        return pd.Series(float("nan"), index=t_s.index)

    values: list[float] = []
    point_idx = 0
    current = float("nan")
    for t in t_s:
        while point_idx < len(points) and points[point_idx][0] <= t:
            current = points[point_idx][1]
            point_idx += 1
        values.append(current)
    return pd.Series(values, index=t_s.index)


def anchor_users_at_decision(decision: AdaptiveDecision) -> int | None:
    """User level entering after this decision (the new flat interval)."""
    if decision.users_after is not None:
        return decision.users_after
    return decision.users_at_step

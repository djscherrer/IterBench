"""Extract load-profile / adaptive-run narrative from ``bench.log``."""

from __future__ import annotations

import re

_ADAPTIVE_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\].*?(baxbench\.adaptive:.*)$"
)


def summarize_load_run(
    bench_log: str,
    *,
    load_profile: str = "",
) -> str:
    if not bench_log.strip():
        return "(no bench.log available)"

    lines: list[str] = []
    if load_profile:
        lines.append(f"- **Load profile**: `{load_profile}`")

    adaptive_lines: list[str] = []
    for raw in bench_log.splitlines():
        m = _ADAPTIVE_RE.search(raw)
        if m:
            adaptive_lines.append(f"- `{m.group(1)}` {m.group(2)}")

    if adaptive_lines:
        lines.append("")
        lines.append(
            "**Adaptive controller** (per step: attempted users, requests, "
            "failures, p95 latency, and step goodput):"
        )
        lines.extend(adaptive_lines)

    if not lines:
        return "(load profile details not found in bench.log)"
    return "\n".join(lines)


def load_profile_from_config(config: dict) -> str:
    profiles = config.get("requested_profiles") or {}
    if isinstance(profiles, dict):
        prof = profiles.get("load_profile")
        if prof:
            return str(prof)
    return str(config.get("resolved_load_profile") or config.get("load_profile") or "")

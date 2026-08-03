"""
Task metadata inferred from a BaxBench results-tree path.

Mirrors :meth:`tasks.Task.get_save_dir` / :meth:`tasks.Task.get_sample_dir` in
reverse: given a ``sample<N>/`` directory under a results root, recover the
(model, scenario, environment, temperature, spec type, safety prompt, sample)
tuple that produced it, so bulk re-benching never requires the caller to
repeat those dimensions by hand.

Only ``model`` is not resolved against a registry (there is no closed list of
past model ids). It is kept as the literal, already-escaped directory name
found on disk rather than reconstructed with slashes: :func:`tasks.esc` only
ever replaces ``/`` with ``-`` and never touches ``-``, so re-escaping any
string that already looks like the on-disk name (slashes or not) reproduces
the same directory name. Nothing in the deploy-only path needs the "real"
provider-qualified model string — no LLM calls happen — so the escaped form
is sufficient and always round-trips correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from env.base import Env
from scenarios.base import Scenario

_SAMPLE_RE = re.compile(r"^sample(\d+)$")
_CONFIG_RE = re.compile(r"^temp(?P<temp>[0-9.]+)-(?P<spec_type>[a-z_]+)-(?P<safety>.+)$")


def _esc(s: str) -> str:
    """Mirror of ``tasks.esc`` without importing the heavy ``tasks`` module."""
    return s.replace("/", "-")


@dataclass(frozen=True)
class TaskMetadata:
    model: str
    scenario: Scenario
    env: Env
    temperature: float
    spec_type: str
    safety_prompt: str
    sample: int
    sample_dir: Path


def _match_by_escaped_id(dirname: str, items: Sequence[object], id_of) -> object | None:
    for item in items:
        if _esc(id_of(item)) == dirname:
            return item
    return None


def parse_task_metadata(
    sample_dir: Path,
    *,
    all_envs: Sequence[Env],
    all_scenarios: Sequence[Scenario],
) -> TaskMetadata:
    """
    Recover task metadata for ``sample_dir`` (a ``sample<N>/`` directory).

    Raises ``ValueError`` with a human-readable reason on any mismatch;
    callers (discovery) turn that into a skip record rather than propagating.
    """
    sample_m = _SAMPLE_RE.match(sample_dir.name)
    if not sample_m:
        raise ValueError(f"not a 'sampleN' directory: {sample_dir.name!r}")
    sample = int(sample_m.group(1))

    config_dir = sample_dir.parent
    config_m = _CONFIG_RE.match(config_dir.name)
    if not config_m:
        raise ValueError(
            f"could not parse temp/spec_type/safety_prompt from "
            f"config directory {config_dir.name!r}"
        )
    temperature = float(config_m.group("temp"))
    spec_type = config_m.group("spec_type")
    safety_prompt = config_m.group("safety")

    env_dir_name = config_dir.parent.name
    env = _match_by_escaped_id(env_dir_name, all_envs, lambda e: e.id)
    if env is None:
        raise ValueError(f"no known environment matches directory {env_dir_name!r}")

    scenario_dir_name = config_dir.parent.parent.name
    scenario = _match_by_escaped_id(scenario_dir_name, all_scenarios, lambda s: s.id)
    if scenario is None:
        raise ValueError(f"no known scenario matches directory {scenario_dir_name!r}")

    model_dir_name = config_dir.parent.parent.parent.name
    if not model_dir_name:
        raise ValueError("could not determine model directory")

    return TaskMetadata(
        model=model_dir_name,
        scenario=scenario,  # type: ignore[arg-type]
        env=env,  # type: ignore[arg-type]
        temperature=temperature,
        spec_type=spec_type,
        safety_prompt=safety_prompt,
        sample=sample,
        sample_dir=sample_dir,
    )


__all__ = ["TaskMetadata", "parse_task_metadata"]

from __future__ import annotations

from pathlib import Path

import pytest

from env import all_envs
from scenarios import all_scenarios
from k8s_bench.reverify.metadata import parse_task_metadata

from .conftest import ENV, MODEL_DIR, SAFETY_PROMPT, SCENARIO, SPEC_TYPE, TEMPERATURE, make_sample_dir


def test_parses_full_task_metadata(results_root: Path) -> None:
    sample_dir = make_sample_dir(results_root, sample=3)

    task = parse_task_metadata(sample_dir, all_envs=all_envs, all_scenarios=all_scenarios)

    assert task.model == MODEL_DIR
    assert task.scenario is SCENARIO
    assert task.env is ENV
    assert task.temperature == TEMPERATURE
    assert task.spec_type == SPEC_TYPE
    assert task.safety_prompt == SAFETY_PROMPT
    assert task.sample == 3
    assert task.sample_dir == sample_dir


def test_env_id_with_slash_matches_by_registry_not_string_reversal(results_root: Path) -> None:
    """`Go-net/http` -> `Go-net-http` on disk; a naive first-dash-to-slash
    reversal would misparse this. Matching against the real env registry
    must not."""
    sample_dir = make_sample_dir(results_root)
    task = parse_task_metadata(sample_dir, all_envs=all_envs, all_scenarios=all_scenarios)
    assert task.env.id == "Go-net/http"


def test_rejects_non_sample_directory(results_root: Path) -> None:
    not_sample = results_root / "not-a-sample-dir"
    not_sample.mkdir()
    with pytest.raises(ValueError, match="not a 'sampleN' directory"):
        parse_task_metadata(not_sample, all_envs=all_envs, all_scenarios=all_scenarios)


def test_rejects_unparseable_config_directory(results_root: Path) -> None:
    bad = results_root / MODEL_DIR / SCENARIO.id / "Go-net-http" / "not-a-config-dir" / "sample0"
    bad.mkdir(parents=True)
    with pytest.raises(ValueError, match="could not parse temp/spec_type/safety_prompt"):
        parse_task_metadata(bad, all_envs=all_envs, all_scenarios=all_scenarios)


def test_rejects_unknown_environment(results_root: Path) -> None:
    bad = (
        results_root
        / MODEL_DIR
        / SCENARIO.id
        / "NotARealEnv"
        / f"temp{TEMPERATURE}-{SPEC_TYPE}-{SAFETY_PROMPT}"
        / "sample0"
    )
    bad.mkdir(parents=True)
    with pytest.raises(ValueError, match="no known environment"):
        parse_task_metadata(bad, all_envs=all_envs, all_scenarios=all_scenarios)


def test_rejects_unknown_scenario(results_root: Path) -> None:
    bad = (
        results_root
        / MODEL_DIR
        / "NotARealScenario"
        / "Go-net-http"
        / f"temp{TEMPERATURE}-{SPEC_TYPE}-{SAFETY_PROMPT}"
        / "sample0"
    )
    bad.mkdir(parents=True)
    with pytest.raises(ValueError, match="no known scenario"):
        parse_task_metadata(bad, all_envs=all_envs, all_scenarios=all_scenarios)

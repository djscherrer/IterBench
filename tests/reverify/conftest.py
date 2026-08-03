"""Shared fixtures for k8s_bench.reverify unit tests.

Builds a synthetic results tree using the *real* env/scenario registries
(``env.all_envs`` / ``scenarios.all_scenarios``) so path <-> metadata
round-tripping is exercised against the same objects production code uses,
without needing Docker, Kubernetes, or any network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from env import all_envs
from scenarios import all_scenarios

# `Go-net/http` is the one env id containing '/', the exact ambiguous case
# `esc()` collapses -- deliberately used here to prove matching-by-registry
# (not string-reversal) resolves it correctly.
ENV = next(e for e in all_envs if e.id == "Go-net/http")
SCENARIO = next(s for s in all_scenarios if s.id == "ClickCount")
MODEL_DIR = "z-ai-glm-5.2"  # an escaped model id with an internal '-' of its own
TEMPERATURE = 0.2
SPEC_TYPE = "openapi"
SAFETY_PROMPT = "high_performance"
EXPERIMENT = "default"


def esc(s: str) -> str:
    return s.replace("/", "-")


def make_sample_dir(results_root: Path, *, model: str = MODEL_DIR, sample: int = 0) -> Path:
    sample_dir = (
        results_root
        / model
        / esc(SCENARIO.id)
        / esc(ENV.id)
        / f"temp{TEMPERATURE}-{SPEC_TYPE}-{SAFETY_PROMPT}"
        / f"sample{sample}"
    )
    sample_dir.mkdir(parents=True, exist_ok=True)
    return sample_dir


def iterations_root_for(sample_dir: Path, *, experiment: str = EXPERIMENT) -> Path:
    root = sample_dir / "k8s-experiments" / experiment / "iterations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_iteration(
    iterations_dir: Path,
    folder_name: str,
    *,
    spec: bool = True,
    code: bool = True,
) -> Path:
    """Create one iteration folder with (optionally) a spec and a code snapshot."""
    path = iterations_dir / folder_name
    (path / "03-spec").mkdir(parents=True, exist_ok=True)
    if spec:
        (path / "03-spec" / "spec.yaml").write_text("namespace: baxbench\n", encoding="utf-8")
    if code:
        code_dir = path / "02-code" / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        (code_dir / "app.py").write_text("# app\n", encoding="utf-8")
    (path / "04-deploy").mkdir(parents=True, exist_ok=True)
    (path / "04-deploy" / "manifests").mkdir(parents=True, exist_ok=True)
    (path / "05-bench").mkdir(parents=True, exist_ok=True)
    return path


def make_complete_bench(iteration_path: Path) -> None:
    """Mark an iteration's 05-bench/ as a finished run (as a copied original would be)."""
    (iteration_path / "05-bench" / "config.json").write_text("{}\n", encoding="utf-8")
    (iteration_path / "05-bench" / "bench.log").write_text("done\n", encoding="utf-8")


@pytest.fixture()
def results_root(tmp_path: Path) -> Path:
    root = tmp_path / "results_reverified"
    root.mkdir()
    return root

"""
LLM-driven generation of ``k8s_configs/<iteration>/spec.yaml`` deployment parameters.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
from pathlib import Path
from typing import Any

import yaml

from env.base import Env
from prompts import Prompter
from scenarios.base import Scenario

from ..feedback import IterationFeedback
from ..cluster.capacity import (
    ClusterCapacity,
    _parse_cpu_to_millicores,
    _parse_memory_to_bytes,
    capacity_as_json,
    collect_cluster_capacity,
)
from .models import BackendSpec, DatabaseSpec, K8sWorkloadSpec, ResourceSpec
from ..paths import iteration_spec_path, new_iteration_id, normalize_iteration_id
from .render import render_iteration
from ..util.sample import functional_tests_gate

_IMAGE_PLACEHOLDER = "baxbench/pending-at-bench:latest"

_BENCHMARK_LOAD_HINT = (
    "The deployment will be exercised by extensive load testing: many concurrent "
    "virtual users, each issuing frequent HTTP requests against the API (create/read "
    "stories, nodes, links, etc.). Size replicas and resources for sustained high "
    "throughput and low error rates under that pressure."
)

_SPEC_BLOCK_RE = re.compile(r"<SPEC>\s*(.*?)\s*</SPEC>", re.DOTALL | re.IGNORECASE)
_YAML_FENCE_RE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _safety_performance_text(
    env: Env, scenario: Scenario, safety_prompt: str
) -> str:
    """Reuse the same performance instruction as application code generation."""
    prompt = scenario.build_prompt(
        env,
        spec_type="openapi",
        safety_prompt=safety_prompt,
        agent=False,
        use_stubs=False,
    )
    for line in prompt.splitlines():
        if "high-workload" in line or "thousands of requests" in line:
            return line.strip()
        if "concurrent users" in line and "performance" in line:
            return line.strip()
    return (
        "Optimize for very high concurrent load and sustainable throughput under benchmark."
    )


def _read_app_hints(code_dir: pathlib.Path, *, max_chars: int = 4000) -> str:
    if not code_dir.is_dir():
        return "(application code not found yet)"
    candidates = ["app.js", "main.py", "app.py", "server.js", "index.js"]
    for name in candidates:
        path = code_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            return f"Excerpt from {name}:\n{text}"
    files = sorted(code_dir.glob("*"))[:5]
    if not files:
        return "(empty code directory)"
    return "Code files: " + ", ".join(p.name for p in files)


def build_k8s_spec_prompt(
    *,
    env: Env,
    scenario: Scenario,
    safety_prompt: str,
    capacity: ClusterCapacity,
    app_hints: str,
    iteration_id: str,
    prior_feedback: IterationFeedback | str | None = None,
) -> str:
    perf = _safety_performance_text(env, scenario, safety_prompt)
    if prior_feedback is not None:
        fb_text = (
            prior_feedback.to_prompt_text()
            if isinstance(prior_feedback, IterationFeedback)
            else str(prior_feedback).strip()
        )
        goal = f"""## Goal
You are refining deployment parameters for iteration `{iteration_id}` after a benchmark of the **previous** iteration.
Use the feedback below to improve replicas and CPU/memory limits. Reduce errors and saturation; increase sustainable throughput if possible.

{perf}

## Feedback from previous benchmark
{fb_text}
"""
    else:
        goal = f"""## Goal
Propose deployment parameters for iteration `{iteration_id}` so the application can sustain **very high concurrent user load** in a Locust benchmark.

{perf}
"""
    return f"""You are a Kubernetes deployment tuning expert for BaxBench performance experiments.

{goal}

## Application
- Scenario: {scenario.id}
- Environment: {env.id} (listen port {env.port})
- Database required: {scenario.needs_db}

{app_hints}

## Cluster capacity (schedulable workers only)
```json
{capacity_as_json(capacity)}
```

## Rules
1. Output **only** a YAML fragment for `backend` and `database` (no manifests, no namespace).
2. Set `backend.replicas` (integer >= 1). Prefer spreading load across workers; do not exceed worker_count * 2.
3. Set `backend.resources` and `database.resources` with valid Kubernetes quantities (`500m`, `1`, `512Mi`, `2Gi`).
4. Keep sum(replicas * cpu_limit) <= budget_cpu_after_reserve and sum(memory limits) <= budget_memory_gi_after_reserve (roughly).
5. Postgres stays a single instance; tune `database.resources` only (enabled stays true).
6. Do **not** set `image`, `port`, `namespace`, or env vars — the framework fills those at bench time.
7. Size Postgres for concurrent connections from all backend pods (connection pools).

## Benchmark load
{_BENCHMARK_LOAD_HINT}

## Output format
Return exactly one block:

<SPEC>
backend:
  replicas: <int>
  resources:
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
database:
  enabled: true
  resources:
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
</SPEC>
"""


def parse_spec_fragment(response: str) -> dict[str, Any]:
    match = _SPEC_BLOCK_RE.search(response)
    text = match.group(1).strip() if match else ""
    if not text:
        fences = _YAML_FENCE_RE.findall(response)
        if fences:
            text = fences[-1].strip()
    if not text:
        raise ValueError("Model response did not contain <SPEC> YAML or a ```yaml``` block")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Parsed spec fragment is not a YAML mapping")
    return data


def _resource_millicores(res: ResourceSpec) -> int:
    return _parse_cpu_to_millicores(res.cpu_limit)


def _resource_memory_bytes(res: ResourceSpec) -> int:
    return _parse_memory_to_bytes(res.memory_limit)


def validate_spec_against_cluster(
    spec: K8sWorkloadSpec,
    capacity: ClusterCapacity,
) -> list[str]:
    warnings: list[str] = []
    if spec.backend.replicas < 1:
        warnings.append("backend.replicas must be >= 1")
    if spec.backend.replicas > max(1, capacity.worker_count * 2):
        warnings.append(
            f"backend.replicas={spec.backend.replicas} is high for {capacity.worker_count} workers"
        )

    backend_cpu = spec.backend.replicas * _resource_millicores(spec.backend.resources)
    db_cpu = _resource_millicores(spec.database.resources)
    if backend_cpu + db_cpu > capacity.budget_cpu_millicores:
        warnings.append(
            f"CPU limits (~{backend_cpu + db_cpu}m) exceed budget {capacity.budget_cpu_millicores}m"
        )

    backend_mem = spec.backend.replicas * _resource_memory_bytes(spec.backend.resources)
    db_mem = _resource_memory_bytes(spec.database.resources)
    if backend_mem + db_mem > capacity.budget_memory_bytes:
        warnings.append(
            "Memory limits exceed cluster budget after reserve"
        )
    return warnings


def merge_fragment_into_spec(
    fragment: dict[str, Any],
    *,
    iteration_id: str,
    app_port: int,
    needs_db: bool,
    labels: dict[str, str],
) -> K8sWorkloadSpec:
    iid = normalize_iteration_id(iteration_id)
    backend_raw = fragment.get("backend") or {}
    if not isinstance(backend_raw, dict):
        raise ValueError("spec fragment must include backend mapping")

    db_raw = fragment.get("database") or {}
    if not isinstance(db_raw, dict):
        db_raw = {}

    backend = BackendSpec(
        image=str(backend_raw.get("image") or _IMAGE_PLACEHOLDER),
        replicas=max(1, int(backend_raw.get("replicas", 1))),
        port=int(backend_raw.get("port") or app_port),
        resources=ResourceSpec.from_mapping(backend_raw.get("resources")),
        env={},
    )
    database = DatabaseSpec.from_mapping(
        {
            "enabled": needs_db if needs_db else bool(db_raw.get("enabled", True)),
            **db_raw,
        }
        if needs_db
        else {"enabled": False}
    )
    return K8sWorkloadSpec(
        iteration_id=iid,
        namespace=f"baxbench-{iid}",
        backend=backend,
        database=database,
        labels=dict(labels),
    )


def generate_k8s_workload_spec(
    *,
    env: Env,
    scenario: Scenario,
    model: str,
    provider: str | None,
    temperature: float,
    reasoning_effort: str,
    safety_prompt: str,
    capacity: ClusterCapacity,
    app_hints: str,
    iteration_id: str,
    logger: logging.Logger,
    vllm_port: int = 8000,
    prior_feedback: IterationFeedback | str | None = None,
) -> tuple[K8sWorkloadSpec, str, list[str]]:
    """Call the configured LLM and return (spec, raw_response, validation_warnings)."""
    prompt = build_k8s_spec_prompt(
        env=env,
        scenario=scenario,
        safety_prompt=safety_prompt,
        capacity=capacity,
        app_hints=app_hints,
        iteration_id=iteration_id,
        prior_feedback=prior_feedback,
    )
    logger.info("k8s spec generation prompt:\n%s", prompt)
    prompter = Prompter(
        env=env,
        scenario=scenario,
        model=model,
        spec_type="openapi",
        safety_prompt=safety_prompt,
        batch_size=1,
        offset=0,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        vllm_port=vllm_port,
        provider=provider,
        use_stubs=False,
    )
    prompter.prompt = prompt
    responses = prompter.prompt_model(logger)
    if not responses:
        raise RuntimeError("LLM returned no completion for k8s spec generation")
    raw = responses[0]
    fragment = parse_spec_fragment(raw)
    spec = merge_fragment_into_spec(
        fragment,
        iteration_id=iteration_id,
        app_port=env.port,
        needs_db=scenario.needs_db,
        labels={},
    )
    warnings = validate_spec_against_cluster(spec, capacity)
    return spec, raw, warnings


def write_spec_generation_artifacts(
    iteration_path: pathlib.Path,
    *,
    spec: K8sWorkloadSpec,
    raw_response: str,
    capacity: ClusterCapacity,
    warnings: list[str],
    logger: logging.Logger,
) -> pathlib.Path:
    iteration_path.mkdir(parents=True, exist_ok=True)
    spec_path = iteration_spec_path(iteration_path)
    spec.write_yaml(spec_path)
    render_iteration(iteration_path)

    meta = {
        "spec_path": str(spec_path),
        "warnings": warnings,
        "cluster_capacity": capacity.to_prompt_dict(),
        "workload_spec": spec.to_yaml_dict(),
    }
    (iteration_path / "spec_gen.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (iteration_path / "spec_gen.log").write_text(
        raw_response + "\n",
        encoding="utf-8",
    )
    if warnings:
        for w in warnings:
            logger.warning("spec validation: %s", w)
    logger.info("Wrote %s and rendered manifests", spec_path)
    return spec_path


def generate_k8s_specs_for_task(
    task: Any,
    results_dir: Path,
    samples: list[int],
    force: bool,
    *,
    k8s_iteration: str | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
    prior_feedback: Any | None = None,
    phase_index: int = 1,
) -> list[Path]:
    from tasks import esc

    written: list[Path] = []
    capacity = collect_cluster_capacity()

    for sample in samples:
        if not functional_tests_gate(task, results_dir, sample):
            continue

        sample_dir = task.get_sample_dir(results_dir, sample)
        iid = normalize_iteration_id(
            k8s_iteration
            or os.environ.get("BAXBENCH_K8S_ITERATION")
            or new_iteration_id(sample_dir)
        )
        iteration_path = task.get_k8s_iteration_dir(results_dir, sample, iid)
        spec_path = iteration_spec_path(iteration_path)
        regen = force or phase_index > 1
        if spec_path.is_file() and not regen:
            logging.getLogger(task.id).info(
                "sample%d: spec exists at %s (use --force to regenerate)",
                sample,
                spec_path,
            )
            written.append(spec_path)
            continue

        log_file = iteration_path / "spec_gen_prompt.log"
        iteration_path.mkdir(parents=True, exist_ok=True)
        with task.create_logger(log_file) as logger:
            code_dir = task.get_code_dir(results_dir, sample)
            app_hints = _read_app_hints(code_dir)
            labels = {
                "baxbench.dev/model": esc(task.model),
                "baxbench.dev/scenario": esc(task.scenario.id),
                "baxbench.dev/env": esc(task.env.id),
                "baxbench.dev/spec-gen": "true",
            }

            retries = 0
            while True:
                try:
                    spec, raw, warnings = generate_k8s_workload_spec(
                        env=task.env,
                        scenario=task.scenario,
                        model=task.model,
                        provider=task.provider,
                        temperature=task.temperature,
                        reasoning_effort=task.reasoning_effort,
                        safety_prompt=task.safety_prompt,
                        capacity=capacity,
                        app_hints=app_hints,
                        iteration_id=iid,
                        logger=logger,
                        vllm_port=vllm_port,
                        prior_feedback=prior_feedback,
                    )
                    spec = K8sWorkloadSpec(
                        iteration_id=spec.iteration_id,
                        namespace=spec.namespace,
                        backend=BackendSpec(
                            image=spec.backend.image,
                            replicas=spec.backend.replicas,
                            port=task.env.port,
                            resources=spec.backend.resources,
                            env=spec.backend.env,
                        ),
                        database=spec.database,
                        labels={**spec.labels, **labels},
                    )
                    out = write_spec_generation_artifacts(
                        iteration_path,
                        spec=spec,
                        raw_response=raw,
                        capacity=capacity,
                        warnings=warnings,
                        logger=logger,
                    )
                    written.append(out)
                    break
                except Exception as e:
                    retries += 1
                    if retries > max_retries:
                        logger.exception(
                            "k8s spec generation failed for sample %d: %s",
                            sample,
                            e,
                            exc_info=e,
                        )
                        break
                    delay = min(base_delay * 2**retries, max_delay)
                    logger.warning(
                        "k8s spec gen retry %d/%d after %s (sleep %.1fs)",
                        retries,
                        max_retries,
                        e,
                        delay,
                    )
                    time.sleep(delay)

    return written

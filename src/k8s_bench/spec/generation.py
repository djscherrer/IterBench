"""
LLM-driven generation of ``iterations/NNN/spec/spec.yaml`` deployment parameters.
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

from ..code_paths import resolve_active_code_dir
from ..feedback import IterationFeedback
from ..cluster.capacity import (
    ClusterCapacity,
    capacity_as_json,
    collect_cluster_capacity,
)
from .models import BackendSpec, DatabaseSpec, K8sWorkloadSpec, ResourceSpec
from .scheduling import (
    SpecValidationError,
    infer_pool_max_from_hints,
    normalize_spec_placement,
    validate_spec_against_cluster,
)
from ..workspace import (
    default_k8s_namespace,
    ensure_iteration_core_layout,
    find_iteration_spec_path,
    iteration_spec_dir,
    iteration_spec_path,
    new_iteration_id,
    normalize_iteration_id,
    resolve_iteration_dir,
)
from .render import render_iteration

_IMAGE_PLACEHOLDER = "baxbench/pending-at-bench:latest"

_BENCHMARK_LOAD_HINT = (
    "The deployment will be exercised by extensive load testing: many concurrent "
    "virtual users, each issuing frequent HTTP requests against the API (create/read "
    "stories, nodes, links, etc.). Size replicas and resources to maximize **goodput** "
    "(sustained rate of **successful** HTTP responses) under that pressure. Raw "
    "throughput that comes with elevated error rates is NOT a win — failed requests "
    "do not count."
)


def _format_iteration_progress(
    *, phase_index: int, total_phases: int
) -> str:
    """Human-friendly progress line, e.g. ``Iteration 4 of 10 (refinement)``."""
    if total_phases <= 0:
        return f"Iteration {phase_index}"
    remaining = max(0, total_phases - phase_index - 1)
    kind = "baseline" if phase_index == 0 else "refinement"
    return (
        f"Iteration {phase_index} of {total_phases - 1} ({kind}); "
        f"{remaining} more iteration(s) remain after this one."
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
    phase_index: int = 0,
    total_phases: int = 0,
    prior_feedback: IterationFeedback | str | None = None,
    validation_feedback: str | None = None,
) -> str:
    perf = _safety_performance_text(env, scenario, safety_prompt)
    pool_max = infer_pool_max_from_hints(app_hints)
    progress = _format_iteration_progress(
        phase_index=phase_index, total_phases=total_phases
    )
    if prior_feedback is not None:
        fb_text = (
            prior_feedback.to_prompt_text()
            if isinstance(prior_feedback, IterationFeedback)
            else str(prior_feedback).strip()
        )
        goal = f"""## Goal
You are refining deployment parameters for iteration `{iteration_id}` after a benchmark of the **previous** iteration.

**Progress**: {progress} Plan your remaining budget — bold experiments early, consolidate refinements toward the end.

Use the feedback below to improve replicas and CPU/memory limits. Reduce errors and saturation; **maximize goodput (sustained rate of successful responses)**. Raw throughput with high error rates does NOT count.

{perf}

## Feedback from previous benchmark
{fb_text}
"""
    else:
        goal = f"""## Goal
Propose deployment parameters for iteration `{iteration_id}` so the application can sustain **very high concurrent user load** in a Locust benchmark while **maximizing goodput (successful responses per second)**. Failed requests do not count toward your score.

**Progress**: {progress}

{perf}
"""
    validation_block = ""
    if validation_feedback:
        validation_block = f"""
{validation_feedback.strip()}

"""
    worker_lines = []
    for w in capacity.worker_nodes:
        worker_lines.append(
            f"- `{w.name}`: allocatable **{w.allocatable_cpu_millicores}m CPU** / "
            f"**{round(w.allocatable_memory_bytes / (2**30), 2)} Gi** memory "
            f"(schedulable; leave ~10% headroom per node)"
        )
    workers_text = "\n".join(worker_lines) if worker_lines else "(no worker nodes reported)"

    return f"""You are a Kubernetes deployment tuning expert for BaxBench performance experiments.

{goal}{validation_block}## Application
- Scenario: {scenario.id}
- Environment: {env.id} (listen port {env.port})
- Database required: {scenario.needs_db}
- App DB connection pool (per replica): **{pool_max}** connections (from generated code)

{app_hints}

## Cluster capacity
Schedulable **workers only** (control-plane excluded). Use **requests** for scheduling fit.

**Cluster budget (sum across workers, after {capacity.suggested_reserve_fraction:.0%} reserve):**
- CPU: {capacity.budget_cpu_millicores}m (~{capacity.budget_cpu_millicores / 1000:.1f} cores)
- Memory: ~{round(capacity.budget_memory_bytes / (2**30), 2)} Gi

**Per-worker capacity (each pod must fit on ONE of these nodes):**
{workers_text}

```json
{capacity_as_json(capacity)}
```

## Scheduling rules (critical — hard limits enforced before deploy)
1. **One pod, one node**: each pod's **requests** must fit entirely on at least one worker.
2. **Connection budget**: `backend.replicas × {pool_max} ≤ database.max_connections` on the **primary** (app pools connect to primary only).
3. **Cluster budget**: sum of all pod requests (backends + all database pods) must fit cluster capacity after reserve.
4. Optional **placement**: restrict or pin which workers may run postgres/backends; `spread_replicas: true` spreads backend pods across nodes.
5. Use worker **`name` values** from the per-worker list (short names like `node3` are accepted).

## Optimization objective
**Maximize goodput** — successful HTTP responses per second sustained over the run. Failed requests (5xx, timeouts, connection errors) are **not counted** as wins. A configuration that processes 200 req/s with 0% errors beats one that processes 500 req/s with 20% errors.

## Spec fields (semantics — you choose values)
Use **benchmark feedback** from prior iterations to refine replicas and resources. The framework validates feasibility; it does not prescribe tuning targets.

**`backend`** (horizontally scalable — many stateless pods):
- `replicas`: pod count behind the Service
- `resources`: per-pod CPU/memory requests & limits (scheduling uses **requests**)
- `placement.workers`: optional node allow-list (omit = any worker)
- `placement.spread_replicas`: prefer spreading pods across nodes (default true)

**`database`** (Postgres):
- `replicas`: `1` = single standalone pod; `N>1` = **1 primary + (N−1) streaming read replicas** (async WAL replication; standard K8s pattern). The generated app connects to the **primary** only (`postgres` Service). A `postgres-read` Service exposes replicas for future read-offloading but is unused by default.
- `max_connections`: primary connection limit (`max_connections` on primary only)
- `resources`: per **database pod** (primary and each replica use the same spec)
- `placement.worker`: pin all DB pods to one node (only if combined requests fit that node)
- `placement.workers`: allow-list of nodes for DB pods

When `database.replicas > 1`, the framework renders a replication-aware Postgres image (Bitnami) with primary Deployment + replica StatefulSet. This mirrors production (primary/replica), not multi-master active-active.

## Rules
1. Output **only** a YAML fragment for `backend` and `database` (no manifests, no namespace).
2. Set `backend.replicas` (integer >= 1).
3. Set `backend.resources` and `database.resources` with valid Kubernetes quantities (`500m`, `1`, `512Mi`, `2Gi`).
4. Keep **sum of requests** (backend replicas × backend requests + database replicas × database requests) within cluster budget.
5. Do **not** set `image`, `port`, `namespace`, or env vars — the framework fills those at bench time.

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
  placement:
    workers: [<worker-name>, ...]   # optional; omit to allow all workers
    spread_replicas: true            # optional; default true
database:
  enabled: true
  replicas: <int>                    # 1 = standalone; N>1 = 1 primary + (N-1) read replicas
  max_connections: <int>             # primary only; must fit backend.replicas × {pool_max}
  resources:
    cpu_request: <quantity>
    cpu_limit: <quantity>
    memory_request: <quantity>
    memory_limit: <quantity>
  placement:
    worker: <worker-name>            # optional; exact pin (preferred for isolation)
    # workers: [<name>, ...]         # optional alternative; allow-list (pick one node)
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


def _parse_backend_placement(backend_raw: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    placement_raw = backend_raw.get("placement") or {}
    workers: list[str] = []
    spread = True
    if isinstance(placement_raw, dict):
        raw_workers = placement_raw.get("workers") or placement_raw.get("worker_nodes") or []
        if isinstance(raw_workers, (list, tuple)):
            workers = [str(w).strip() for w in raw_workers if str(w).strip()]
        spread = bool(placement_raw.get("spread_replicas", True))
    return tuple(workers), spread


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

    placement_workers, spread_replicas = _parse_backend_placement(backend_raw)
    backend = BackendSpec(
        image=str(backend_raw.get("image") or _IMAGE_PLACEHOLDER),
        replicas=max(1, int(backend_raw.get("replicas", 1))),
        port=int(backend_raw.get("port") or app_port),
        resources=ResourceSpec.from_mapping(backend_raw.get("resources")),
        env={},
        placement_workers=placement_workers,
        spread_replicas=spread_replicas,
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
        namespace=default_k8s_namespace(iid),
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
    validation_feedback: str | None = None,
    max_validation_retries: int = 3,
    sample_dir: pathlib.Path | None = None,
    iteration_path: pathlib.Path | None = None,
    phase_index: int = 0,
    total_phases: int = 0,
) -> tuple[K8sWorkloadSpec, str, list[str]]:
    """Call the configured LLM and return (spec, raw_response, validation_warnings)."""
    last_raw = ""
    validation_hint = validation_feedback
    for attempt in range(1, max_validation_retries + 1):
        prompt = build_k8s_spec_prompt(
            env=env,
            scenario=scenario,
            safety_prompt=safety_prompt,
            capacity=capacity,
            app_hints=app_hints,
            iteration_id=iteration_id,
            phase_index=phase_index,
            total_phases=total_phases,
            prior_feedback=prior_feedback,
            validation_feedback=validation_hint,
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
        if sample_dir is not None:
            from ..llm_cost import record_k8s_llm_call

            record_k8s_llm_call(
                prompter=prompter,
                call_type="k8s_spec_generation",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=iteration_spec_dir(iteration_path) if iteration_path else None,
                iteration_id=iteration_id,
                note=f"validation_attempt={attempt}",
            )
        if not responses:
            raise RuntimeError("LLM returned no completion for k8s spec generation")
        last_raw = responses[0]
        fragment = parse_spec_fragment(last_raw)
        spec = merge_fragment_into_spec(
            fragment,
            iteration_id=iteration_id,
            app_port=env.port,
            needs_db=scenario.needs_db,
            labels={},
        )
        spec, placement_errors = normalize_spec_placement(spec, capacity)
        if placement_errors:
            validation_hint = SpecValidationError(placement_errors).to_prompt_text()
            logger.warning(
                "spec validation attempt %d/%d failed (placement): %s",
                attempt,
                max_validation_retries,
                placement_errors,
            )
            continue

        result = validate_spec_against_cluster(
            spec, capacity, app_hints=app_hints
        )
        if result.errors:
            validation_hint = SpecValidationError(result.errors).to_prompt_text()
            logger.warning(
                "spec validation attempt %d/%d failed: %s",
                attempt,
                max_validation_retries,
                result.errors,
            )
            continue

        if attempt > 1:
            logger.info("spec validation passed on attempt %d", attempt)
        return spec, last_raw, result.warnings

    raise SpecValidationError(
        [f"Spec still invalid after {max_validation_retries} generation attempt(s)."]
        + [ln for ln in (validation_hint or "").splitlines() if ln.strip()][-8:]
    )


def write_spec_generation_artifacts(
    iteration_path: pathlib.Path,
    *,
    spec: K8sWorkloadSpec,
    raw_response: str,
    capacity: ClusterCapacity,
    warnings: list[str],
    logger: logging.Logger,
) -> pathlib.Path:
    ensure_iteration_core_layout(iteration_path)
    spec_path = iteration_spec_path(iteration_path)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec.write_yaml(spec_path)
    render_iteration(iteration_path)

    meta = {
        "spec_path": str(spec_path),
        "warnings": warnings,
        "cluster_capacity": capacity.to_prompt_dict(),
        "workload_spec": spec.to_yaml_dict(),
    }
    spec_dir = iteration_spec_dir(iteration_path)
    (spec_dir / "spec_gen.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (spec_dir / "spec_gen.log").write_text(
        raw_response + "\n",
        encoding="utf-8",
    )
    if warnings:
        for w in warnings:
            logger.warning("spec validation: %s", w)
    logger.info("Wrote %s and rendered manifests", spec_path)
    return spec_path


def reuse_deployment_spec_for_iteration(
    *,
    iteration_path: Path,
    sample_dir: Path,
    source_iteration_id: str,
    target_iteration_id: str,
    extra_labels: dict[str, str] | None = None,
    logger: logging.Logger,
) -> Path:
    """
    Copy deployment parameters from a prior iteration (no spec LLM).

    Used after successful **code** refinement: bench the new image under the
    same replicas/resources/DB settings as the iteration we learned from.
    """
    source_path = resolve_iteration_dir(sample_dir, source_iteration_id)
    src_spec_path = find_iteration_spec_path(source_path)
    if src_spec_path is None:
        raise FileNotFoundError(
            f"No spec to reuse under {source_path} (from {source_iteration_id!r})"
        )

    spec = K8sWorkloadSpec.from_yaml_file(src_spec_path)
    iid = normalize_iteration_id(target_iteration_id)
    labels = dict(spec.labels)
    if extra_labels:
        labels.update(extra_labels)

    reused = K8sWorkloadSpec(
        iteration_id=iid,
        namespace=default_k8s_namespace(iid),
        backend=spec.backend,
        database=spec.database,
        labels=labels,
    )

    ensure_iteration_core_layout(iteration_path)
    dest = iteration_spec_path(iteration_path)
    reused.write_yaml(dest)
    render_iteration(iteration_path)

    note = (
        f"Reused deployment spec from {source_path.name} ({source_iteration_id})\n"
        f"Target iteration: {iid}\n"
        "No LLM spec generation (code-only refinement phase).\n"
    )
    (iteration_spec_dir(iteration_path) / "spec_reused_from.txt").write_text(
        note, encoding="utf-8"
    )
    logger.info(
        "Reused deployment spec from %s → %s (no LLM spec generation)",
        source_path,
        dest,
    )
    return dest


def generate_k8s_specs_for_task(
    task: Any,
    results_dir: Path,
    samples: list[int],
    force: bool,
    *,
    k8s_iteration: str | None = None,
    iteration_path: Path | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    vllm_port: int = 8000,
    prior_feedback: Any | None = None,
    phase_index: int = 1,
) -> list[Path]:
    """LLM spec per sample. Callers must run ``functional_tests_gate`` before calling."""
    from tasks import esc

    written: list[Path] = []
    capacity = collect_cluster_capacity()

    for sample in samples:
        sample_dir = task.get_sample_dir(results_dir, sample)
        iid = normalize_iteration_id(
            k8s_iteration
            or os.environ.get("BAXBENCH_K8S_ITERATION")
            or new_iteration_id(sample_dir)
        )
        if iteration_path is None:
            from k8s_bench.workspace import resolve_iteration_dir

            iteration_path = resolve_iteration_dir(sample_dir, iid)
            if not iteration_path.is_dir():
                iteration_path = task.get_k8s_iteration_dir(results_dir, sample, iid)
        ensure_iteration_core_layout(iteration_path)
        spec_path = iteration_spec_path(iteration_path)
        regen = force or phase_index > 0
        if find_iteration_spec_path(iteration_path) is not None and not regen:
            existing = find_iteration_spec_path(iteration_path)
            assert existing is not None
            logging.getLogger(task.id).info(
                "sample%d: spec exists at %s (use --force to regenerate)",
                sample,
                existing,
            )
            written.append(existing)
            continue

        log_file = iteration_spec_dir(iteration_path) / "spec_gen_prompt.log"
        with task.create_logger(log_file) as logger:
            code_dir = resolve_active_code_dir(
                task=task, results_dir=results_dir, sample=sample
            )
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
                        sample_dir=sample_dir,
                        iteration_path=iteration_path,
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
                            placement_workers=spec.backend.placement_workers,
                            spread_replicas=spec.backend.spread_replicas,
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
                    try:
                        from ..experiment_summary import append_spec_generation_block

                        summary_path = append_spec_generation_block(
                            sample_dir=sample_dir,
                            iteration_id=iid,
                            iteration_path=iteration_path,
                            spec=spec,
                            raw_response=raw,
                            warnings=warnings,
                            had_prior_feedback=prior_feedback is not None,
                            phase_index=phase_index,
                        )
                        logger.info("Updated experiment summary: %s", summary_path)
                    except Exception as exc:
                        logger.warning("Could not update experiment summary: %s", exc)
                    written.append(out)
                    break
                except SpecValidationError as e:
                    logger.error(
                        "k8s spec validation failed for sample %d after LLM retries: %s",
                        sample,
                        e,
                    )
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


def _apply_task_labels_to_spec(
    spec: K8sWorkloadSpec,
    *,
    task: Any,
    results_dir: Path,
    sample: int,
) -> K8sWorkloadSpec:
    from tasks import esc

    labels = {
        "baxbench.dev/model": esc(task.model),
        "baxbench.dev/scenario": esc(task.scenario.id),
        "baxbench.dev/env": esc(task.env.id),
        "baxbench.dev/spec-gen": "true",
    }
    return K8sWorkloadSpec(
        iteration_id=spec.iteration_id,
        namespace=spec.namespace,
        backend=BackendSpec(
            image=spec.backend.image,
            replicas=spec.backend.replicas,
            port=task.env.port,
            resources=spec.backend.resources,
            env=spec.backend.env,
            placement_workers=spec.backend.placement_workers,
            spread_replicas=spec.backend.spread_replicas,
        ),
        database=spec.database,
        labels={**spec.labels, **labels},
    )


def generate_and_write_spec(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    iteration_id: str,
    logger: logging.Logger,
    capacity: ClusterCapacity,
    prior_feedback: IterationFeedback | None = None,
    validation_feedback: str | None = None,
    max_validation_retries: int = 1,
    phase_index: int = 0,
    total_phases: int = 0,
    vllm_port: int = 8000,
) -> tuple[Path | None, str | None]:
    """
    Generate spec via LLM, static-validate, write artifacts.

    Returns ``(spec_path, error_message)``. ``error_message`` is set when static
    validation fails after ``max_validation_retries`` attempt(s).
    """
    sample_dir = task.get_sample_dir(results_dir, sample)
    code_dir = resolve_active_code_dir(task=task, results_dir=results_dir, sample=sample)
    app_hints = _read_app_hints(code_dir)
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
            iteration_id=iteration_id,
            logger=logger,
            vllm_port=vllm_port,
            prior_feedback=prior_feedback,
            validation_feedback=validation_feedback,
            max_validation_retries=max_validation_retries,
            sample_dir=sample_dir,
            iteration_path=iteration_path,
            phase_index=phase_index,
            total_phases=total_phases,
        )
        spec = _apply_task_labels_to_spec(
            spec, task=task, results_dir=results_dir, sample=sample
        )
        out = write_spec_generation_artifacts(
            iteration_path,
            spec=spec,
            raw_response=raw,
            capacity=capacity,
            warnings=warnings,
            logger=logger,
        )
        try:
            from ..experiment_summary import append_spec_generation_block

            append_spec_generation_block(
                sample_dir=sample_dir,
                iteration_id=iteration_id,
                iteration_path=iteration_path,
                spec=spec,
                raw_response=raw,
                warnings=warnings,
                had_prior_feedback=prior_feedback is not None,
                phase_index=phase_index,
            )
        except Exception as exc:
            logger.warning("Could not update experiment summary: %s", exc)
        return out, None
    except SpecValidationError as exc:
        return None, exc.to_prompt_text()
    except Exception as exc:
        logger.exception("spec generation failed: %s", exc, exc_info=exc)
        return None, str(exc)


def generate_baseline_spec_until_deployable(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    iteration_id: str,
    logger: logging.Logger,
    deploy_probe: Any,
    phase_index: int = 0,
    total_phases: int = 0,
    vllm_port: int = 8000,
    max_deploy_attempts: int | None = None,
) -> tuple[Path | None, str | None]:
    """
    Baseline (iteration-000): retry spec generation until deploy probe passes.

    ``deploy_probe`` is a zero-arg callable returning ``DeployProbeResult``.
    """
    if max_deploy_attempts is None:
        max_deploy_attempts = int(
            os.environ.get("BAXBENCH_K8S_BASELINE_SPEC_MAX_ATTEMPTS", "5")
        )
    capacity = collect_cluster_capacity()
    validation_feedback: str | None = None
    last_error = "baseline spec generation did not produce a deployable configuration"

    for attempt in range(1, max_deploy_attempts + 1):
        logger.info(
            "baseline spec attempt %d/%d for sample %d",
            attempt,
            max_deploy_attempts,
            sample,
        )
        spec_path, gen_error = generate_and_write_spec(
            task=task,
            results_dir=results_dir,
            sample=sample,
            iteration_path=iteration_path,
            iteration_id=iteration_id,
            logger=logger,
            capacity=capacity,
            prior_feedback=None,
            validation_feedback=validation_feedback,
            max_validation_retries=3,
            phase_index=phase_index,
            total_phases=total_phases,
            vllm_port=vllm_port,
        )
        if spec_path is None:
            last_error = gen_error or last_error
            validation_feedback = gen_error
            continue

        probe = deploy_probe()
        if probe.ok:
            logger.info(
                "baseline deploy probe passed on attempt %d for sample %d",
                attempt,
                sample,
            )
            return spec_path, None

        last_error = probe.reason
        validation_feedback = probe.to_prompt_feedback()
        logger.warning(
            "baseline deploy probe failed attempt %d/%d: %s",
            attempt,
            max_deploy_attempts,
            probe.reason,
        )

    return None, last_error


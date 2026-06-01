"""Regenerate application code using k8s benchmark feedback; re-run functional tests."""

from __future__ import annotations

import logging
import multiprocessing
import random
import shutil
import time
from pathlib import Path
from typing import Any

import docker

from prompts import Parser, Prompter

from ..feedback import IterationFeedback
from ..functional_failure import FunctionalFailureReport
from ..workspace import (
    PROMPT_LOG_FILENAME,
    RESPONSE_LOG_FILENAME,
    find_iteration_spec_path,
    image_id_from_test_log,
    iteration_code_phase_dir,
    iteration_code_snapshot_dir,
    iteration_folder_is_failed,
    iteration_functional_tests_dir,
    iteration_id_for_index,
    iterations_root,
    latest_code_dir,
    latest_spec_path,
    load_failure_report,
    parse_iteration_folder_name,
    parse_iteration_index,
)
from ..spec.models import K8sWorkloadSpec

# Hard cap on the rendered code block so very large multi-file projects
# do not blow the LLM context. 200 KB ≈ 50K tokens for typical code, which
# fits comfortably in modern long-context models while keeping every file
# visible. Override with ``BAXBENCH_K8S_CODE_REFINE_MAX_CHARS``.
_DEFAULT_CODE_BUDGET_CHARS = 200_000

# Decision phase uses the same "all files" rendering as refinement, but with a
# tighter budget — the decision LLM only emits a short ``<DECISION>`` +
# ``<RATIONALE>`` (a few hundred tokens) and we run it once per iteration, so
# we don't want to pay refinement-sized input costs there. 80 KB (~20K tokens)
# fits virtually every BaxBench sample's full app source today while leaving
# room for the benchmark feedback table and failure block in the same prompt.
# Override with ``BAXBENCH_K8S_DECISION_CODE_MAX_CHARS``.
_DEFAULT_DECISION_CODE_BUDGET_CHARS = 80_000

# Files we never include in the prompt (build/dependency artifacts, caches,
# lockfiles, binary blobs, hidden metadata).
_CODE_IGNORE_NAMES = frozenset({
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".cache",
    ".idea",
    ".vscode",
})
_CODE_IGNORE_SUFFIXES = frozenset({
    ".lock",
    ".log",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".class",
    ".jar",
    ".zip",
    ".tar",
    ".gz",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
    ".sqlite",
    ".db",
})


def _iter_code_files(code_dir: Path) -> list[Path]:
    """Return all source files under ``code_dir`` in stable, prioritized order."""
    if not code_dir.is_dir():
        return []
    priority_first = ("app.js", "app.py", "main.py", "main.rs", "server.js", "index.js")
    found: list[Path] = []
    for p in code_dir.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _CODE_IGNORE_NAMES for part in p.relative_to(code_dir).parts):
            continue
        if p.suffix.lower() in _CODE_IGNORE_SUFFIXES:
            continue
        found.append(p)

    def _sort_key(path: Path) -> tuple[int, str]:
        name = path.name
        try:
            return (priority_first.index(name), str(path))
        except ValueError:
            return (len(priority_first), str(path))

    return sorted(found, key=_sort_key)


def _render_code_files(code_dir: Path, *, budget_chars: int) -> str:
    """
    Concatenate all source files under ``code_dir`` using ``<FILEPATH>`` /
    ``<CODE>`` blocks (same format the refinement LLM must emit back), capped
    at ``budget_chars`` total characters.

    Files are emitted in :func:`_iter_code_files` priority order until the
    budget is exhausted; remaining files are listed by name so the model knows
    what was skipped (and that they exist in the codebase).
    """
    files = _iter_code_files(code_dir)
    if not files:
        return "(application code not found yet)"

    blocks: list[str] = []
    skipped: list[str] = []
    used = 0
    for path in files:
        rel = path.relative_to(code_dir).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append(f"{rel} (unreadable)")
            continue
        block = f"<FILEPATH>\n{rel}\n</FILEPATH>\n<CODE>\n{content}\n</CODE>\n"
        if used + len(block) > budget_chars and blocks:
            skipped.append(rel)
            continue
        blocks.append(block)
        used += len(block)

    body = "\n".join(blocks).rstrip()
    if skipped:
        body += (
            "\n\n(Additional files in the codebase, not shown due to context "
            "budget — assume they exist unchanged: "
            + ", ".join(skipped)
            + ")"
        )
    return body


def _read_full_code_for_refinement(
    code_dir: Path, *, budget_chars: int | None = None
) -> str:
    """Full multi-file code dump for the *code-refinement* LLM (large budget)."""
    import os

    if budget_chars is None:
        budget_chars = int(
            os.environ.get(
                "BAXBENCH_K8S_CODE_REFINE_MAX_CHARS",
                str(_DEFAULT_CODE_BUDGET_CHARS),
            )
        )
    return _render_code_files(code_dir, budget_chars=budget_chars)


def _read_full_code_for_decision(
    code_dir: Path, *, budget_chars: int | None = None
) -> str:
    """
    Full multi-file code dump for the *decision* LLM (deployment vs code).

    Same rendering as refinement so the decision agent sees exactly the source
    the next refinement would see — just with a tighter default cap because
    decision is invoked once per iteration and emits a short rationale, not a
    full code rewrite.
    """
    import os

    if budget_chars is None:
        budget_chars = int(
            os.environ.get(
                "BAXBENCH_K8S_DECISION_CODE_MAX_CHARS",
                str(_DEFAULT_DECISION_CODE_BUDGET_CHARS),
            )
        )
    return _render_code_files(code_dir, budget_chars=budget_chars)


def _resolve_active_spec(
    iteration_path: Path, sample_dir: Path
) -> tuple[K8sWorkloadSpec, Path, Path] | None:
    """
    Return ``(spec, spec_path, iteration_dir)`` for the deployment shape that
    will be used when this iteration runs.

    Resolution order:
    1. The current iteration's own ``spec/spec.yaml`` if already on disk
       (rare at code-refinement time — the spec stage runs *after* code).
    2. The newest on-disk spec from any prior iteration (successful or failed),
       via :func:`workspace.latest_spec_path`.
    """
    own = find_iteration_spec_path(iteration_path)
    if own is not None:
        try:
            return K8sWorkloadSpec.from_yaml_file(own), own, iteration_path
        except Exception:
            pass

    latest = latest_spec_path(sample_dir)
    if latest is None:
        return None
    spec_path, source_dir = latest
    try:
        return K8sWorkloadSpec.from_yaml_file(spec_path), spec_path, source_dir
    except Exception:
        return None


def _format_k8s_deployment_context(
    iteration_path: Path, sample_dir: Path
) -> str:
    """
    Render a "K8s deployment context" block for the code refinement prompt.

    The block exposes the deployment shape the LLM is about to be benchmarked
    under (replicas, Postgres ``max_connections``, resources, env vars). This
    is the bridge that lets code refinement reason about per-pod connection
    pool size against the database connection budget — without it, the code
    pass and the spec pass talk past each other.

    Returns an empty string when no spec is available anywhere on disk yet
    (e.g. iteration 0 baseline before the first deploy).
    """
    resolved = _resolve_active_spec(iteration_path, sample_dir)
    if resolved is None:
        return ""
    spec, _spec_path, source_dir = resolved
    backend = spec.backend
    db = spec.database

    lines: list[str] = [
        "### K8s deployment context (read-only here — set by the spec stage)",
        "",
        (
            "The application will run under the deployment below. **Treat this "
            "as a binding constraint when sizing per-pod resources in code** "
            "(notably DB connection pool size, worker counts, in-memory caches). "
            "Don't reshape the deployment — change the code to fit it."
        ),
        "",
        f"- **Source spec**: `{source_dir.name}` (most recent on disk; "
        "may be from a failed iteration if benchmark didn't run)",
        f"- **Backend replicas**: {backend.replicas}",
        f"- **Backend resources**: cpu {backend.resources.cpu_request}/"
        f"{backend.resources.cpu_limit}, "
        f"mem {backend.resources.memory_request}/{backend.resources.memory_limit}",
    ]
    if db.enabled:
        topology = (
            f"1 primary + {db.replicas - 1} read replica(s) (streaming replication)"
            if db.replicas > 1
            else "single primary"
        )
        lines.extend(
            [
                f"- **Postgres replicas**: {db.replicas} ({topology})",
                f"- **Postgres `max_connections`**: {db.max_connections}",
                f"- **Postgres resources**: cpu {db.resources.cpu_request}/"
                f"{db.resources.cpu_limit}, "
                f"mem {db.resources.memory_request}/{db.resources.memory_limit}",
            ]
        )
    else:
        lines.append("- **Database**: disabled")

    if backend.env:
        env_entries = ", ".join(
            f"`{k}={v}`" for k, v in sorted(backend.env.items())
        )
        lines.append(f"- **Backend env**: {env_entries}")
    else:
        lines.append("- **Backend env**: (none injected by spec)")

    if db.enabled:
        # Concrete budget hint. We don't enforce it; the LLM decides how to
        # spend the budget (smaller pool, fewer in-flight transactions, etc.).
        budget = max(db.max_connections - 10, 1)  # leave a few admin slots
        per_pod = max(budget // max(backend.replicas, 1), 1)
        lines.extend(
            [
                "",
                (
                    "**Concurrency budget**: Postgres allows "
                    f"{db.max_connections} connections. With {backend.replicas} "
                    f"backend replica(s), each pod's DB connection pool should "
                    f"target **≤ {per_pod}** clients "
                    f"(≈ ({db.max_connections} − admin slots) / replicas). "
                    "If the pool size is currently hardcoded, prefer "
                    "`process.env.PG_POOL_MAX` (or equivalent) so the K8s "
                    "spec can tune it without another code rewrite."
                ),
            ]
        )
        if db.replicas > 1:
            lines.append(
                f"- For this iteration `DB_READ_HOST` is set and points to "
                f"{db.replicas - 1} read replica(s). Route read-only queries "
                "(GET endpoints, simulate, export) through it; keep writes on "
                "the primary."
            )

    return "\n".join(lines)


def iteration_functional_tests_passed(iteration_path: Path) -> bool:
    from ..util.sample import functional_tests_passed_at

    return functional_tests_passed_at(
        iteration_functional_tests_dir(iteration_path) / "test_results.json"
    )


def find_latest_prior_failure_report(
    sample_dir: Path,
    *,
    current_iteration_index: int,
) -> FunctionalFailureReport | None:
    """
    Return the ``failure_report.json`` from the most recent
    ``iteration-XXX-code-failed`` strictly before ``current_iteration_index``.

    This is the bridge that ensures the next code refinement attempt is given
    the exact tests + errors of the previous one, not a generic FailedAttempt
    excerpt buried at the bottom of the prompt.
    """
    root = iterations_root(sample_dir)
    if not root.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if not iteration_folder_is_failed(child.name):
            continue
        if "-code" not in child.name:
            continue
        idx = parse_iteration_index(child.name)
        if idx is None or idx >= current_iteration_index:
            continue
        if best is None or idx > best[0]:
            best = (idx, child)
    if best is None:
        return None
    return load_failure_report(best[1])


def build_code_refinement_prompt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    prior_feedback: IterationFeedback,
    same_iteration_failure_report: FunctionalFailureReport | None = None,
    prior_failure_report: FunctionalFailureReport | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
) -> str:
    base = task.scenario.build_prompt(
        task.env,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        agent=False,
        use_stubs=task.use_stubs,
    )
    code_dir = latest_code_dir(
        task.get_sample_dir(results_dir, sample),
        fallback=task.get_code_dir(results_dir, sample),
    )
    full_code = _read_full_code_for_refinement(code_dir)

    from ..spec.generation import _format_iteration_progress

    progress = _format_iteration_progress(
        iteration_index=iteration_index, total_iterations=total_iterations
    )
    parts = [
        base,
        "",
        "## Refinement task (k8s benchmark feedback)",
        "",
        f"**Progress**: {progress} Budget your remaining iterations accordingly — pick the changes most likely to lift goodput within what is left.",
        "",
        "You previously generated this application. A Kubernetes Locust benchmark was run "
        "with a deployment spec (see below). Improve the **application source code** to "
        "**maximize goodput** (sustained rate of *successful* HTTP responses). Reduce errors, "
        "timeouts, and inefficiencies revealed by the benchmark — raw throughput that comes "
        "with elevated error rates is NOT a win, because failed requests do not count.",
        "",
        "Keep the same API contract and scenario requirements. Output a complete replacement "
        "codebase using the same `<FILEPATH>` / `<CODE>` format as initial generation.",
        "",
    ]
    replica_hint = _format_k8s_deployment_context(
        iteration_path, task.get_sample_dir(results_dir, sample)
    )
    if replica_hint:
        parts.extend([replica_hint, ""])

    # Failed-FT context from the previous code-refinement attempt goes BEFORE
    # the benchmark feedback. Correctness is a precondition for any goodput
    # gain; if last attempt broke `func_test_simulate_…`, we want the LLM to
    # see that and the actual error before it starts reasoning about p95 / CPU.
    if prior_failure_report is not None:
        prior_block = prior_failure_report.to_prompt_block()
        if prior_block:
            parts.extend(
                [
                    "### Previous code-refinement attempt failed (this is a "
                    "**must-fix** signal — do not produce another revision "
                    "that breaks the same tests)",
                    "",
                    prior_block,
                    "",
                ]
            )

    # If we just rendered a dedicated failure block for an iteration, drop the
    # matching ``FailedAttempt`` from the benchmark-feedback section so the LLM
    # does not see the same failure twice (cleaner prompt, no behaviour change).
    feedback_for_prompt = prior_feedback
    if prior_failure_report is not None:
        from dataclasses import replace

        skip_id = prior_failure_report.iteration_id
        filtered = tuple(
            a for a in prior_feedback.failed_attempts if a.iteration_id != skip_id
        )
        if len(filtered) != len(prior_feedback.failed_attempts):
            feedback_for_prompt = replace(prior_feedback, failed_attempts=filtered)

    parts.extend(
        [
            "### Benchmark feedback",
            feedback_for_prompt.to_prompt_text(),
            "",
            "### Current application code (full contents — rewrite as needed)",
            full_code,
        ]
    )
    if same_iteration_failure_report is not None:
        same_block = same_iteration_failure_report.to_prompt_block()
        if same_block:
            # Same-iteration retry context (gated on
            # ``BAXBENCH_K8S_CODE_REFINE_MAX_ATTEMPTS > 1``). Same renderer as
            # the cross-iteration block; the heading clarifies which attempt the
            # report is about so the LLM does not confuse the two.
            parts.extend(
                [
                    "",
                    "### Functional test feedback from this iteration's previous codegen attempt",
                    "(your most recent regeneration within this same iteration failed these tests; fix them)",
                    "",
                    same_block,
                ]
            )
    return "\n".join(parts)


def _ensure_docker_network() -> None:
    client = docker.from_env()
    if not [n for n in client.networks.list() if n.name == "baxbench-net"]:
        client.networks.create(name="baxbench-net", driver="bridge")


def _write_code_files(files: dict[Path, str], code_dir: Path) -> None:
    if code_dir.is_dir():
        shutil.rmtree(code_dir)
    code_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        dest = code_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def run_functional_tests_for_iteration(
    task: Any,
    iteration_path: Path,
    *,
    code_dir: Path,
    timeout: int,
    num_ports: int,
    min_port: int,
) -> bool:
    from tasks import SlotManager

    ft_dir = iteration_functional_tests_dir(iteration_path)
    _ensure_docker_network()
    with multiprocessing.Manager() as manager:
        port_manager = SlotManager(manager, num_ports, min_port)
        return task.test_functional_tests_at(
            code_dir=code_dir,
            ft_dir=ft_dir,
            port_manager=port_manager,
            timeout=timeout,
        )


def regenerate_iteration_code(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    prior_feedback: IterationFeedback,
    same_iteration_failure_report: FunctionalFailureReport | None,
    logger: logging.Logger,
    vllm_port: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    prior_failure_report: FunctionalFailureReport | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
) -> bool:
    """Call LLM once to rewrite code under ``iteration_path/code/`` only."""
    if task.use_openhands or task.use_claude_agent:
        logger.warning(
            "Code refinement uses single-prompt Prompter; agent modes are not supported "
            "for k8s code refinement yet"
        )

    prompt = build_code_refinement_prompt(
        task=task,
        results_dir=results_dir,
        sample=sample,
        iteration_path=iteration_path,
        prior_feedback=prior_feedback,
        same_iteration_failure_report=same_iteration_failure_report,
        prior_failure_report=prior_failure_report,
        iteration_index=iteration_index,
        total_iterations=total_iterations,
    )
    sample_dir = task.get_sample_dir(results_dir, sample)
    # Phase folder (``02-code/``) owns the LLM transcript + FT outcome; the
    # actual application source lives in the ``code/`` sub-folder below it.
    # Keeping these separate is what lets ``_write_code_files`` rmtree the
    # snapshot dir between attempts without nuking our prompt/response logs.
    phase_dir = iteration_code_phase_dir(iteration_path)
    phase_dir.mkdir(parents=True, exist_ok=True)
    code_dir = iteration_code_snapshot_dir(iteration_path)
    code_dir.mkdir(parents=True, exist_ok=True)
    refine_log = phase_dir / PROMPT_LOG_FILENAME
    refine_log.write_text(prompt + "\n", encoding="utf-8")
    logger.info("code refinement prompt written to %s", refine_log)

    prompter = Prompter(
        env=task.env,
        scenario=task.scenario,
        model=task.model,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        batch_size=1,
        offset=sample,
        temperature=task.temperature,
        reasoning_effort=task.reasoning_effort,
        vllm_port=vllm_port,
        provider=task.provider,
        use_stubs=task.use_stubs,
    )
    prompter.prompt = prompt

    retries = 0
    while True:
        try:
            from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

            check_k8s_llm_budget(sample_dir)
            responses = prompter.prompt_model(logger)
            idx, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
            iter_id = (
                iteration_id_for_index(idx)
                if idx is not None
                else iteration_path.name
            )
            record_k8s_llm_call(
                prompter=prompter,
                call_type="code_refinement",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=phase_dir,
                iteration_id=iter_id,
                note=f"attempt={retries + 1}",
            )
            if not responses:
                raise RuntimeError("LLM returned no completion for code refinement")
            raw = responses[0]
            (phase_dir / RESPONSE_LOG_FILENAME).write_text(
                raw + "\n", encoding="utf-8"
            )
            files = Parser(task.env, logger).parse_response(raw)
            if Path("failed") in files:
                raise ValueError("Could not parse code refinement response")
            _write_code_files(files, code_dir)
            logger.info(
                "Saved refined code for sample %d under %s (sample dir unchanged)",
                sample,
                code_dir,
            )
            return True
        except Exception as exc:
            retries += 1
            if retries > max_retries:
                logger.error("Code refinement generation failed: %s", exc)
                return False
            delay = min(base_delay * 2**retries, max_delay)
            delay = random.uniform(0, delay)
            logger.warning(
                "Code refinement LLM attempt %d/%d failed: %s; retry in %.1fs",
                retries,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)


def refine_code_until_passing(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    prior_feedback: IterationFeedback,
    logger: logging.Logger,
    ft_timeout: int,
    num_ports: int,
    min_port: int,
    vllm_port: int = 8000,
    max_codegen_attempts: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    prior_failure_report: FunctionalFailureReport | None = None,
    iteration_index: int = 0,
    total_iterations: int = 0,
) -> str | None:
    """
    Regenerate code under the iteration workspace until functional tests pass.

    Sample-level ``code/`` and ``functional_tests/`` are never modified.

    ``prior_failure_report``, when provided, comes from the most recent
    ``iteration-XXX-code-failed`` iteration and is rendered as a dedicated block
    at the top of the refinement prompt so the LLM sees exactly which tests
    failed last time (and which still passed) before reading benchmark stats.

    Returns new docker image id (sha256:…) or None on failure.
    """
    import os

    if max_codegen_attempts is None:
        max_codegen_attempts = int(os.environ.get("BAXBENCH_K8S_CODE_REFINE_MAX_ATTEMPTS", "1"))

    code_dir = iteration_code_snapshot_dir(iteration_path)
    same_iteration_report: FunctionalFailureReport | None = None
    for attempt in range(1, max_codegen_attempts + 1):
        logger.info(
            "code refinement attempt %d/%d for sample %d (iteration=%s, prior_failure=%s)",
            attempt,
            max_codegen_attempts,
            sample,
            iteration_path.name,
            (
                prior_failure_report.iteration_id
                if prior_failure_report is not None
                else "(none)"
            ),
        )
        if not regenerate_iteration_code(
            task=task,
            results_dir=results_dir,
            sample=sample,
            iteration_path=iteration_path,
            prior_feedback=prior_feedback,
            same_iteration_failure_report=same_iteration_report,
            prior_failure_report=prior_failure_report,
            logger=logger,
            vllm_port=vllm_port,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            iteration_index=iteration_index,
            total_iterations=total_iterations,
        ):
            return None

        passed = run_functional_tests_for_iteration(
            task,
            iteration_path,
            code_dir=code_dir,
            timeout=ft_timeout,
            num_ports=num_ports,
            min_port=min_port,
        )
        if passed:
            image_id = image_id_from_test_log(
                iteration_functional_tests_dir(iteration_path) / "test.log"
            )
            logger.info(
                "code refinement succeeded on attempt %d; image=%s",
                attempt,
                image_id,
            )
            return image_id

        # Build the same structured report we use cross-iteration; the next
        # codegen retry (if any) reads it via ``same_iteration_failure_report``.
        from ..functional_failure import build_functional_failure_report

        same_iteration_report = build_functional_failure_report(
            iteration_path, logger=logger
        )
        logger.warning(
            "functional tests failed after code refinement attempt %d/%d (%d/%d FT passed)",
            attempt,
            max_codegen_attempts,
            same_iteration_report.num_passed_ft,
            same_iteration_report.num_total_ft,
        )

    if iteration_functional_tests_passed(iteration_path):
        return image_id_from_test_log(
            iteration_functional_tests_dir(iteration_path) / "test.log"
        )
    return None


# Backward-compatible aliases for imports elsewhere in the repo.
regenerate_sample_code = regenerate_iteration_code

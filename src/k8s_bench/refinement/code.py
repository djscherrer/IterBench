"""Regenerate application code using k8s benchmark feedback; re-run functional tests."""

from __future__ import annotations

import json
import logging
import multiprocessing
import random
import shutil
import time
from pathlib import Path
from typing import Any

import docker

from prompts import Parser, Prompter

from ..code_paths import (
    resolve_active_code_dir,
    resolve_image_id_from_ft_log,
)
from ..feedback import IterationFeedback
from ..workspace import (
    find_iteration_spec_path,
    iteration_code_snapshot_dir,
    iteration_functional_tests_dir,
    iteration_id_for_phase,
    k8s_workspace_root,
    parse_iteration_folder_name,
)
from ..spec.models import K8sWorkloadSpec
from ..util.sample import functional_tests_gate

# Hard cap on the rendered code block so very large multi-file projects
# do not blow the LLM context. 200 KB ≈ 50K tokens for typical code, which
# fits comfortably in modern long-context models while keeping every file
# visible. Override with ``BAXBENCH_K8S_CODE_REFINE_MAX_CHARS``.
_DEFAULT_CODE_BUDGET_CHARS = 200_000

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


def _read_full_code_for_refinement(
    code_dir: Path, *, budget_chars: int | None = None
) -> str:
    """
    Concatenate all source files under ``code_dir`` using ``<FILEPATH>`` /
    ``<CODE>`` blocks (same format the LLM must emit back). Files are emitted
    until ``budget_chars`` is exhausted; remaining files are listed by name so
    the model knows what was skipped.
    """
    import os

    if budget_chars is None:
        budget_chars = int(
            os.environ.get(
                "BAXBENCH_K8S_CODE_REFINE_MAX_CHARS",
                str(_DEFAULT_CODE_BUDGET_CHARS),
            )
        )

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
            "budget — keep them unchanged unless your refinement truly "
            "requires editing them: "
            + ", ".join(skipped)
            + ")"
        )
    return body


def _read_replica_hint_for_iteration(iteration_path: Path) -> str:
    """Brief note when the current deployment actually exposes read replicas."""
    spec_path = find_iteration_spec_path(iteration_path)
    if spec_path is None:
        return ""
    try:
        spec = K8sWorkloadSpec.from_yaml_file(spec_path)
    except Exception:
        return ""
    if not spec.database.enabled or spec.database.replicas <= 1:
        return ""
    return (
        f"Note: for this iteration `DB_READ_HOST` is set and points to "
        f"{spec.database.replicas - 1} read replica(s). Consider whether "
        "additional read endpoints should be routed through it."
    )

PENDING_CODE_REFINEMENT_FILENAME = "pending_code_refinement.json"


def record_pending_code_refinement(
    sample_dir: Path,
    *,
    failed_iteration_id: str,
    reason: str,
) -> Path:
    """Signal the next decision phase that code still needs to pass functional tests."""
    root = k8s_workspace_root(sample_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / PENDING_CODE_REFINEMENT_FILENAME
    path.write_text(
        json.dumps(
            {
                "failed_iteration_id": failed_iteration_id,
                "reason": reason,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def clear_pending_code_refinement(sample_dir: Path) -> None:
    path = k8s_workspace_root(sample_dir) / PENDING_CODE_REFINEMENT_FILENAME
    if path.is_file():
        path.unlink()


def pending_code_refinement_hint_text(sample_dir: Path) -> str:
    path = k8s_workspace_root(sample_dir) / PENDING_CODE_REFINEMENT_FILENAME
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    failed = str(data.get("failed_iteration_id", ""))
    reason = str(data.get("reason", ""))
    if not failed:
        return ""
    return (
        "## Pending code fix\n\n"
        f"The previous phase (`{failed}`) attempted **code** refinement but "
        f"functional tests did **not** pass. {reason}\n\n"
        "You should strongly consider choosing **`code`** again unless benchmark "
        "evidence clearly points to deployment limits only.\n"
    )


def iteration_functional_tests_passed(iteration_path: Path) -> bool:
    path = iteration_functional_tests_dir(iteration_path) / "test_results.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    passed = int(data.get("num_passed_ft", 0))
    total = int(data.get("num_total_ft", 0))
    return total > 0 and passed >= total


def format_iteration_functional_test_feedback(
    task: Any,
    iteration_path: Path,
) -> str:
    ft_dir = iteration_functional_tests_dir(iteration_path)
    path = ft_dir / "test_results.json"
    if not path.is_file():
        return "(no functional test results yet)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(unreadable functional test results)"

    lines = [
        f"Summary: {data.get('num_passed_ft', 0)}/{data.get('num_total_ft', 0)} "
        f"functional tests passed",
        f"Exceptions during tests: {data.get('num_ft_exceptions', 0)}",
    ]
    for ft in task.scenario.functional_tests:
        log_path = ft_dir / f"{ft.__name__}.log"
        if log_path.is_file():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            excerpt = "\n".join(tail[-20:])
            lines.extend(["", f"### {ft.__name__} log (tail)", "```", excerpt, "```"])
    test_log = ft_dir / "test.log"
    if test_log.is_file():
        tail = test_log.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.extend(["", "### test.log (tail)", "```", "\n".join(tail[-30:]), "```"])
    return "\n".join(lines)


def build_code_refinement_prompt(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    iteration_path: Path,
    prior_feedback: IterationFeedback,
    functional_test_feedback: str | None = None,
    phase_index: int = 0,
    total_phases: int = 0,
) -> str:
    base = task.scenario.build_prompt(
        task.env,
        spec_type=task.spec_type,
        safety_prompt=task.safety_prompt,
        agent=False,
        use_stubs=task.use_stubs,
    )
    code_dir = resolve_active_code_dir(task=task, results_dir=results_dir, sample=sample)
    full_code = _read_full_code_for_refinement(code_dir)

    from ..spec.generation import _format_iteration_progress

    progress = _format_iteration_progress(
        phase_index=phase_index, total_phases=total_phases
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
    replica_hint = _read_replica_hint_for_iteration(iteration_path)
    if replica_hint:
        parts.extend([replica_hint, ""])
    parts.extend(
        [
            "### Benchmark feedback",
            prior_feedback.to_prompt_text(),
            "",
            "### Current application code (full contents — rewrite as needed)",
            full_code,
        ]
    )
    if functional_test_feedback:
        parts.extend(
            [
                "",
                "### Functional test feedback (must pass after your changes)",
                functional_test_feedback,
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
    functional_test_feedback: str | None,
    logger: logging.Logger,
    vllm_port: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    phase_index: int = 0,
    total_phases: int = 0,
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
        functional_test_feedback=functional_test_feedback,
        phase_index=phase_index,
        total_phases=total_phases,
    )
    sample_dir = task.get_sample_dir(results_dir, sample)
    code_dir = iteration_code_snapshot_dir(iteration_path)
    code_dir.mkdir(parents=True, exist_ok=True)
    refine_log = code_dir / "refinement_prompt.log"
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
            responses = prompter.prompt_model(logger)
            from ..llm_cost import record_k8s_llm_call

            phase, _kind, _failed = parse_iteration_folder_name(iteration_path.name)
            iter_id = (
                iteration_id_for_phase(phase)
                if phase is not None
                else iteration_path.name
            )
            record_k8s_llm_call(
                prompter=prompter,
                call_type="code_refinement",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=code_dir,
                iteration_id=iter_id,
                note=f"attempt={retries + 1}",
            )
            if not responses:
                raise RuntimeError("LLM returned no completion for code refinement")
            raw = responses[0]
            (code_dir / "refinement_response.log").write_text(raw + "\n", encoding="utf-8")
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
    phase_index: int = 0,
    total_phases: int = 0,
) -> str | None:
    """
    Regenerate code under the iteration workspace until functional tests pass.

    Sample-level ``code/`` and ``functional_tests/`` are never modified.

    Returns new docker image id (sha256:…) or None on failure.
    """
    import os

    if max_codegen_attempts is None:
        max_codegen_attempts = int(os.environ.get("BAXBENCH_K8S_CODE_REFINE_MAX_ATTEMPTS", "1"))

    code_dir = iteration_code_snapshot_dir(iteration_path)
    ft_feedback: str | None = None
    for attempt in range(1, max_codegen_attempts + 1):
        logger.info(
            "code refinement attempt %d/%d for sample %d (iteration=%s)",
            attempt,
            max_codegen_attempts,
            sample,
            iteration_path.name,
        )
        if not regenerate_iteration_code(
            task=task,
            results_dir=results_dir,
            sample=sample,
            iteration_path=iteration_path,
            prior_feedback=prior_feedback,
            functional_test_feedback=ft_feedback,
            logger=logger,
            vllm_port=vllm_port,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            phase_index=phase_index,
            total_phases=total_phases,
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
            image_id = resolve_image_id_from_ft_log(
                iteration_functional_tests_dir(iteration_path) / "test.log"
            )
            logger.info(
                "code refinement succeeded on attempt %d; image=%s",
                attempt,
                image_id,
            )
            return image_id

        ft_feedback = format_iteration_functional_test_feedback(task, iteration_path)
        logger.warning(
            "functional tests failed after code refinement attempt %d/%d",
            attempt,
            max_codegen_attempts,
        )

    if iteration_functional_tests_passed(iteration_path):
        return resolve_image_id_from_ft_log(
            iteration_functional_tests_dir(iteration_path) / "test.log"
        )
    return None


# Backward-compatible aliases for imports elsewhere in the repo.
regenerate_sample_code = regenerate_iteration_code

def functional_tests_passed(task: Any, results_dir: Path, sample: int) -> bool:
    """Sample-level FT gate (initial codegen only)."""
    path = task.get_test_results_json_path(results_dir, sample)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    passed = int(data.get("num_passed_ft", 0))
    total = int(data.get("num_total_ft", 0))
    return total > 0 and passed >= total

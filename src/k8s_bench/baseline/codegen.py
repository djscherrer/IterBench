"""
Baseline (iteration-000) code generation with retry-on-FT-failure.

Used when ``--baseline-code regenerate`` is set: instead of reusing the
sample-level ``code/`` snapshot produced by ``--mode generate``, the framework
calls the scenario prompt against the configured LLM for **this** experiment
run, validates the result with the functional-test suite, and lands the
working code under ``iteration-000-baseline/02-code/code/``. Failed attempts
are preserved verbatim under ``iteration-000-baseline/02-code/attempts/<NNN>/``
so a human can audit the prompt, response, generated code, and FT logs for any
attempt that did not converge.

The sample-level ``code/`` directory is **never** modified — keeping it as the
deterministic reuse baseline for ``--baseline-code reuse`` runs.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import random
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker

from prompts import Parser, Prompter

from ..util.sample import (
    append_k8s_skip,
    ensure_docker_image,
    functional_tests_passed_at,
)
from ..workspace import (
    PROMPT_LOG_FILENAME,
    RESPONSE_LOG_FILENAME,
    attempt_subdir,
    baseline_codegen_meta_path,
    ensure_iteration_core_layout,
    image_id_from_test_log,
    iteration_code_attempts_dir,
    iteration_code_log_path,
    iteration_code_phase_dir,
    iteration_code_snapshot_dir,
    iteration_folder_with_suffix,
    iteration_functional_tests_dir,
    iteration_id_for_index,
    iterations_root,
    mark_iteration_folder_failed,
    next_attempt_index,
)


@dataclass(frozen=True)
class BaselineCodegenResult:
    """Outcome of :func:`run_baseline_codegen`."""

    iteration_path: Path
    code_dir: Path
    image_id: str
    attempts_used: int
    reused_existing: bool


# Filenames written inside each ``attempts/<NNN>/`` directory. They mirror the
# top-level ``02-code/`` layout (prompt/response transcript + code snapshot +
# FT outputs) so a reader does not need to know which attempt won — every
# attempt is self-contained.
_ATTEMPT_META_FILENAME = "attempt.json"


# Substring matches in ``functional_tests/test.log`` that indicate the FT
# harness itself broke (not the code under test). When we see one of these,
# regenerating code with a new LLM call is pointless — the underlying host
# environment needs human attention. The codegen loop bails out immediately
# instead of burning the remaining attempts, and the hint is surfaced in
# both ``attempt.json`` and ``codegen.json`` so the operator can see *why*
# without grepping the multi-megabyte ``test.log`` themselves.
_INFRA_FAILURE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "port is already allocated",
        "host port already in use — likely a stale baxbench container from a "
        "previous run. Clean with: docker ps -a --filter name=baxbench- "
        "--format '{{.Names}}' | grep -v '^baxbench-registry$' | xargs -r "
        "docker rm -f",
    ),
    (
        "Bind for 0.0.0.0:",
        "host port bind failed (see test.log for the exact port and "
        "underlying Docker error)",
    ),
    (
        "Cannot connect to the Docker daemon",
        "Docker daemon unreachable (check `docker info` / restart the daemon)",
    ),
    (
        "no space left on device",
        "host disk full — free space and retry",
    ),
    (
        "OCI runtime create failed",
        "container runtime error (see test.log for runc/containerd details)",
    ),
)


def _read_log_tail(path: Path, *, max_chars: int = 32_000) -> str:
    """Best-effort tail read of a (potentially large) text log."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def _classify_ft_failure(ft_dir: Path) -> tuple[bool, str, str]:
    """
    Decide whether the failed FT run is an infra/host issue (not retryable).

    Returns ``(is_infra_failure, hint, log_excerpt)``:

    - ``is_infra_failure``: when ``True`` the codegen retry loop should bail
      immediately — regenerating code won't fix a stale Docker port or a dead
      daemon.
    - ``hint``: short human-readable explanation suitable for an operator.
    - ``log_excerpt``: last ~2 KB of ``test.log`` (regardless of classification)
      to embed in ``attempt.json`` so the failure is auditable without
      drilling into the per-attempt directory.

    The heuristic is intentionally conservative: only well-known infra-only
    substrings trigger ``is_infra_failure=True``. Image build failures
    (``Failed to build docker image``), app-startup crashes, FT assertion
    failures, and any ambiguous "all FTs raised exceptions" cases all stay
    classified as code failures — those are exactly what the retry loop is
    designed to recover from.
    """
    log_text = _read_log_tail(ft_dir / "test.log")
    excerpt = log_text[-2_000:] if log_text else ""
    for needle, hint in _INFRA_FAILURE_PATTERNS:
        if needle in log_text:
            return True, hint, excerpt
    return False, "", excerpt


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_docker_network() -> None:
    """Same private helper as refinement.code; create the bridge if missing."""
    client = docker.from_env()
    if not [n for n in client.networks.list() if n.name == "baxbench-net"]:
        client.networks.create(name="baxbench-net", driver="bridge")


def _write_code_files(files: dict[Path, str], code_dir: Path) -> None:
    """Replace ``code_dir`` with the parsed files (LLM rewrite semantics)."""
    if code_dir.is_dir():
        shutil.rmtree(code_dir)
    code_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        dest = code_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _baseline_iteration_path(sample_dir: Path) -> Path:
    """Return ``iteration-000-baseline/`` (creating the parent if needed)."""
    root = iterations_root(sample_dir)
    root.mkdir(parents=True, exist_ok=True)
    folder = iteration_folder_with_suffix("000", "baseline")
    return root / folder


def _existing_passing_codegen(iteration_path: Path) -> tuple[Path, str] | None:
    """
    Return ``(code_dir, image_id)`` when iteration-000 already has a passing
    baseline regenerate-mode codegen on disk, else ``None``.

    The check is intentionally strict: ``codegen.json`` must report
    ``status: passed``, ``02-code/code/`` must be non-empty, and the FT
    artifacts must mark all tests as passing. Anything else triggers a fresh
    attempt loop (with ``--force``-style semantics carried by the caller).
    """
    meta_path = baseline_codegen_meta_path(iteration_path)
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict) or meta.get("status") != "passed":
        return None
    code_dir = iteration_code_snapshot_dir(iteration_path)
    if not code_dir.is_dir() or not any(code_dir.iterdir()):
        return None
    ft_results = iteration_functional_tests_dir(iteration_path) / "test_results.json"
    if not functional_tests_passed_at(ft_results):
        return None
    test_log = iteration_functional_tests_dir(iteration_path) / "test.log"
    image_id = image_id_from_test_log(test_log)
    if image_id is None:
        return None
    return code_dir, image_id


def _rotate_top_level_into_attempt(
    iteration_path: Path,
    attempt_dir: Path,
) -> None:
    """
    Move ``02-code/`` top-level artifacts of the *current* attempt into the
    matching ``attempts/<NNN>/`` subdir before the next attempt rewrites them.

    Only the artifacts that the **current** attempt produced are moved
    (``prompt.log``, ``response.log``, ``code/``, ``functional_tests/``); the
    ``attempts/`` directory itself and any other phase metadata are preserved.
    """
    phase_dir = iteration_code_phase_dir(iteration_path)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name in (PROMPT_LOG_FILENAME, RESPONSE_LOG_FILENAME):
        src = phase_dir / name
        if src.is_file():
            shutil.move(str(src), str(attempt_dir / name))
    for sub in ("code", "functional_tests"):
        src = phase_dir / sub
        if src.is_dir():
            dest = attempt_dir / sub
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))


def _write_attempt_meta(
    attempt_dir: Path,
    *,
    attempt_index: int,
    status: str,
    error: str | None,
    num_passed_ft: int | None,
    num_total_ft: int | None,
    duration_s: float,
    note: str | None = None,
    infra_failure: bool = False,
    error_excerpt: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "attempt_index": attempt_index,
        "status": status,
        "error": error,
        "num_passed_ft": num_passed_ft,
        "num_total_ft": num_total_ft,
        "duration_s": round(duration_s, 3),
        "finished_at": _utc_now(),
    }
    if note:
        payload["note"] = note
    if infra_failure:
        payload["infra_failure"] = True
    if error_excerpt:
        payload["error_excerpt"] = error_excerpt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / _ATTEMPT_META_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_attempt_meta_for_summary(
    attempts_dir: Path,
) -> list[dict[str, Any]]:
    """Collected ``attempt.json`` payloads (oldest first) for ``codegen.json``."""
    if not attempts_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    entries: list[tuple[int, Path]] = []
    for child in attempts_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            n = int(child.name)
        except ValueError:
            continue
        entries.append((n, child))
    for _, attempt_dir in sorted(entries):
        meta_path = attempt_dir / _ATTEMPT_META_FILENAME
        if not meta_path.is_file():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _write_codegen_meta(
    iteration_path: Path,
    *,
    status: str,
    attempts_used: int,
    max_attempts: int,
    task: Any,
    winning_attempt: int | None,
    error: str | None = None,
    infra_failure: bool = False,
) -> Path:
    """Persist ``02-code/codegen.json`` — the canonical baseline-codegen record."""
    attempts = _read_attempt_meta_for_summary(iteration_code_attempts_dir(iteration_path))
    payload: dict[str, Any] = {
        "status": status,
        "attempts_used": attempts_used,
        "max_attempts": max_attempts,
        "winning_attempt": winning_attempt,
        "error": error,
        "model": task.model,
        "provider": task.provider,
        "temperature": task.temperature,
        "reasoning_effort": task.reasoning_effort,
        "spec_type": task.spec_type,
        "safety_prompt": task.safety_prompt,
        "scenario": task.scenario.id,
        "env": task.env.id,
        "use_stubs": task.use_stubs,
        "finished_at": _utc_now(),
        "attempts": attempts,
    }
    if infra_failure:
        payload["infra_failure"] = True
    path = baseline_codegen_meta_path(iteration_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _llm_call_for_baseline(
    *,
    task: Any,
    sample: int,
    vllm_port: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    logger: logging.Logger,
) -> tuple[str, str, Prompter]:
    """
    Single-shot LLM call returning ``(prompt_text, raw_response, prompter)``.

    Wraps :class:`Prompter` with the same exponential-backoff envelope used by
    :func:`k8s_bench.refinement.code.regenerate_iteration_code` so transient
    provider errors don't waste an attempt slot. Prompter's constructor builds
    the canonical scenario prompt, which is exactly the legacy
    ``--mode generate`` prompt — what makes this a baseline (not a refinement).
    """
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
    prompt_text = prompter.prompt

    retries = 0
    while True:
        try:
            responses = prompter.prompt_model(logger)
            if not responses:
                raise RuntimeError("LLM returned no completion for baseline code gen")
            return prompt_text, responses[0], prompter
        except Exception as exc:
            retries += 1
            if retries > max_retries:
                logger.error("Baseline codegen LLM call failed after retries: %s", exc)
                raise
            delay = min(base_delay * 2**retries, max_delay)
            delay = random.uniform(0, delay)
            logger.warning(
                "Baseline codegen LLM attempt %d/%d failed: %s; retry in %.1fs",
                retries,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)


def _run_functional_tests(
    task: Any,
    *,
    code_dir: Path,
    ft_dir: Path,
    ft_timeout: int,
    num_ports: int,
    min_port: int,
) -> bool:
    """Build ``code_dir`` and run FTs into ``ft_dir`` (same pattern as refinement)."""
    from tasks import SlotManager

    _ensure_docker_network()
    with multiprocessing.Manager() as manager:
        port_manager = SlotManager(manager, num_ports, min_port)
        return task.test_functional_tests_at(
            code_dir=code_dir,
            ft_dir=ft_dir,
            port_manager=port_manager,
            timeout=ft_timeout,
        )


def _ft_pass_counts(ft_dir: Path) -> tuple[int, int]:
    """Return ``(passed, total)`` from ``ft_dir/test_results.json`` (or ``0,0``)."""
    p = ft_dir / "test_results.json"
    if not p.is_file():
        return 0, 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0
    return int(data.get("num_passed_ft", 0)), int(data.get("num_total_ft", 0))


def run_baseline_codegen(
    *,
    task: Any,
    results_dir: Path,
    sample: int,
    sample_dir: Path,
    save_dir: Path,
    max_attempts: int,
    ft_timeout: int,
    num_ports: int,
    min_port: int,
    vllm_port: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    force: bool,
) -> BaselineCodegenResult | None:
    """
    Generate baseline application code with retry-on-FT-failure.

    Always lands the final code at ``iteration-000-baseline/02-code/code/`` —
    *never* writes back to the sample-level ``code/`` directory. Returns
    ``None`` on terminal failure (max attempts exhausted, or LLM/provider
    errors that can't be recovered); the iteration folder is then renamed
    ``iteration-000-baseline-code-failed`` so downstream stages skip it.

    When ``02-code/codegen.json`` already exists with ``status: passed`` and a
    valid local image, this function is a no-op (reuse-without-regen) unless
    ``force`` is ``True`` — that mirrors the existing ``--force`` semantics
    used elsewhere in the k8s loop.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    iteration_path = _baseline_iteration_path(sample_dir)
    if iteration_path.is_dir() and not force:
        existing = _existing_passing_codegen(iteration_path)
        if existing is not None:
            code_dir, image_id = existing
            # Confirm the cached image is still in Docker (or rebuild from the
            # iteration snapshot if it was pruned). This is the same
            # ``ensure_docker_image`` contract preflight uses for reused
            # sample-level builds.
            sample_logger = logging.getLogger(task.id)
            resolved = ensure_docker_image(
                task,
                results_dir,
                sample,
                image_id,
                sample_logger,
                code_dir=code_dir,
            )
            if resolved is not None:
                sample_logger.info(
                    "Baseline regenerate-mode codegen: reusing existing passing "
                    "iteration-000 (image=%s)",
                    resolved,
                )
                return BaselineCodegenResult(
                    iteration_path=iteration_path,
                    code_dir=code_dir,
                    image_id=resolved,
                    attempts_used=0,
                    reused_existing=True,
                )

    # Fresh attempt loop. ``ensure_iteration_core_layout`` is idempotent and
    # creates the spec/deploy/bench folders we'll need later anyway.
    ensure_iteration_core_layout(iteration_path)
    phase_dir = iteration_code_phase_dir(iteration_path)
    phase_dir.mkdir(parents=True, exist_ok=True)

    if force:
        # Reset the attempt history so retries don't accumulate across runs.
        attempts_root = iteration_code_attempts_dir(iteration_path)
        if attempts_root.is_dir():
            shutil.rmtree(attempts_root)
        for child in phase_dir.iterdir():
            if child.name == "attempts":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    log_file = iteration_code_log_path(iteration_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    last_error: str | None = None
    winning_attempt: int | None = None
    terminal_infra_failure: bool = False

    with task.create_logger(log_file) as logger:
        logger.info(
            "Baseline regenerate-mode codegen for sample %d (max_attempts=%d, "
            "iteration=%s)",
            sample,
            max_attempts,
            iteration_path.name,
        )

        for attempt_idx in range(1, max_attempts + 1):
            started_at = time.time()
            logger.info(
                "baseline codegen attempt %d/%d for sample %d",
                attempt_idx,
                max_attempts,
                sample,
            )

            from ..llm_cost import check_k8s_llm_budget, record_k8s_llm_call

            try:
                check_k8s_llm_budget(sample_dir)
            except Exception as exc:
                last_error = f"LLM budget exceeded: {exc}"
                logger.error(last_error)
                break

            # 1. LLM call → top-level transcript files. Each attempt's
            # prompt+response are also written into ``attempts/<NNN>/`` after
            # the FT result is known (see _rotate_top_level_into_attempt on
            # failure, or copies on success).
            try:
                prompt_text, raw_response, prompter = _llm_call_for_baseline(
                    task=task,
                    sample=sample,
                    vllm_port=vllm_port,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    max_delay=max_delay,
                    logger=logger,
                )
            except Exception as exc:
                last_error = f"LLM call failed: {exc}"
                # Persist what little we have for this attempt so the failure
                # is still auditable. Attempt dir gets prompt-only (no response).
                attempt_dir = attempt_subdir(
                    iteration_code_attempts_dir(iteration_path), attempt_idx
                )
                attempt_dir.mkdir(parents=True, exist_ok=True)
                _write_attempt_meta(
                    attempt_dir,
                    attempt_index=attempt_idx,
                    status="llm_failed",
                    error=last_error,
                    num_passed_ft=None,
                    num_total_ft=None,
                    duration_s=time.time() - started_at,
                )
                continue

            (phase_dir / PROMPT_LOG_FILENAME).write_text(
                prompt_text + "\n", encoding="utf-8"
            )
            (phase_dir / RESPONSE_LOG_FILENAME).write_text(
                raw_response + "\n", encoding="utf-8"
            )
            record_k8s_llm_call(
                prompter=prompter,
                call_type="baseline_code_generation",
                sample_dir=sample_dir,
                logger=logger,
                artifact_dir=phase_dir,
                iteration_id=iteration_id_for_index(0),
                note=f"attempt={attempt_idx}",
            )

            # 2. Parse + write generated source. Any parse failure is fatal
            # for this attempt; rotate and try again with a fresh LLM call.
            files = Parser(task.env, logger).parse_response(raw_response)
            if Path("failed") in files:
                last_error = "parse failure (LLM response did not contain expected code blocks)"
                logger.warning(last_error)
                attempt_dir = attempt_subdir(
                    iteration_code_attempts_dir(iteration_path), attempt_idx
                )
                _rotate_top_level_into_attempt(iteration_path, attempt_dir)
                _write_attempt_meta(
                    attempt_dir,
                    attempt_index=attempt_idx,
                    status="parse_failed",
                    error=last_error,
                    num_passed_ft=None,
                    num_total_ft=None,
                    duration_s=time.time() - started_at,
                )
                continue

            code_dir = iteration_code_snapshot_dir(iteration_path)
            _write_code_files(files, code_dir)
            logger.info("baseline codegen attempt %d: wrote %s", attempt_idx, code_dir)

            # 3. Functional tests against the freshly generated source. The
            # build + FT image is recorded inside ``functional_tests/test.log``;
            # we read the sha256 back to reuse it for the deploy probe.
            ft_dir = iteration_functional_tests_dir(iteration_path)
            try:
                passed = _run_functional_tests(
                    task,
                    code_dir=code_dir,
                    ft_dir=ft_dir,
                    ft_timeout=ft_timeout,
                    num_ports=num_ports,
                    min_port=min_port,
                )
            except Exception as exc:
                # The harness raised before producing pass/fail counts.
                # ``test.log`` may still hold useful context (port-bind error,
                # daemon down, ...) so classify it before deciding to bail.
                is_infra = False
                hint = ""
                excerpt = ""
                ft_dir_for_exc = iteration_functional_tests_dir(iteration_path)
                if ft_dir_for_exc.is_dir():
                    is_infra, hint, excerpt = _classify_ft_failure(ft_dir_for_exc)

                detailed = f"functional-test runner crashed: {exc!r}"
                last_error = (
                    f"{detailed} — infra failure: {hint}" if is_infra else detailed
                )
                if is_infra:
                    logger.error(
                        "baseline codegen attempt %d/%d: infra failure detected "
                        "during FT harness startup (%s) — aborting retry loop",
                        attempt_idx,
                        max_attempts,
                        hint,
                    )
                else:
                    logger.exception(detailed, exc_info=exc)

                attempt_dir = attempt_subdir(
                    iteration_code_attempts_dir(iteration_path), attempt_idx
                )
                _rotate_top_level_into_attempt(iteration_path, attempt_dir)
                _write_attempt_meta(
                    attempt_dir,
                    attempt_index=attempt_idx,
                    status="infra_failed" if is_infra else "ft_runner_failed",
                    error=last_error,
                    num_passed_ft=None,
                    num_total_ft=None,
                    duration_s=time.time() - started_at,
                    infra_failure=is_infra,
                    error_excerpt=excerpt or None,
                )
                if is_infra:
                    terminal_infra_failure = True
                    break
                continue

            num_passed, num_total = _ft_pass_counts(ft_dir)
            if passed and num_total > 0 and num_passed >= num_total:
                # 4. Winning attempt. Top-level ``02-code/`` already has the
                # winning prompt/response/code/FTs — no need to duplicate them
                # under ``attempts/<N>/``. ``codegen.json.winning_attempt``
                # records the index (== ``attempts_used``) so a reader knows
                # whether prior failed attempts exist alongside.
                winning_attempt = attempt_idx
                image_id = image_id_from_test_log(ft_dir / "test.log")
                if image_id is None:
                    # FTs passed but we couldn't recover the image sha — fall
                    # back to a fresh build from the code snapshot so deploy
                    # still has something to launch.
                    sample_logger = logging.getLogger(task.id)
                    image_id = task._build_image_from_code_dir(code_dir, sample_logger)
                if image_id is None:
                    last_error = (
                        "baseline FTs passed but could not resolve a docker image id"
                    )
                    logger.error(last_error)
                    break
                logger.info(
                    "baseline codegen succeeded on attempt %d (image=%s, "
                    "FT=%d/%d passing)",
                    attempt_idx,
                    image_id,
                    num_passed,
                    num_total,
                )
                _write_codegen_meta(
                    iteration_path,
                    status="passed",
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    task=task,
                    winning_attempt=winning_attempt,
                )
                try:
                    from ..experiment_summary import (
                        append_baseline_codegen_block,
                    )

                    append_baseline_codegen_block(
                        sample_dir=sample_dir,
                        iteration_path=iteration_path,
                        task=task,
                        attempts_used=attempt_idx,
                        max_attempts=max_attempts,
                        winning_attempt=winning_attempt,
                        status="passed",
                        error=None,
                    )
                except Exception as sum_exc:
                    logger.warning(
                        "Could not append baseline codegen block to "
                        "experiment summary: %s",
                        sum_exc,
                    )
                return BaselineCodegenResult(
                    iteration_path=iteration_path,
                    code_dir=code_dir,
                    image_id=image_id,
                    attempts_used=attempt_idx,
                    reused_existing=False,
                )

            # 5. Failed attempt. Before writing the attempt meta, classify
            # the failure: if ``test.log`` shows a known infra-only error
            # (stale port, dead daemon, full disk, ...) there's no point
            # retrying — we'd just burn LLM credits on a problem the host
            # operator needs to fix. Surface the hint up to ``attempt.json``
            # / ``codegen.json`` and break out of the loop.
            is_infra, hint, excerpt = _classify_ft_failure(ft_dir)
            base_error = (
                f"functional tests failed ({num_passed}/{num_total} passing)"
            )
            last_error = f"{base_error} — infra failure: {hint}" if is_infra else base_error
            if is_infra:
                logger.error(
                    "baseline codegen attempt %d/%d: infra failure (%s) — "
                    "aborting retry loop instead of burning LLM calls",
                    attempt_idx,
                    max_attempts,
                    hint,
                )
            else:
                logger.warning(
                    "baseline codegen attempt %d/%d failed: %s",
                    attempt_idx,
                    max_attempts,
                    last_error,
                )
            attempt_dir = attempt_subdir(
                iteration_code_attempts_dir(iteration_path), attempt_idx
            )
            _rotate_top_level_into_attempt(iteration_path, attempt_dir)
            _write_attempt_meta(
                attempt_dir,
                attempt_index=attempt_idx,
                status="infra_failed" if is_infra else "ft_failed",
                error=last_error,
                num_passed_ft=num_passed,
                num_total_ft=num_total,
                duration_s=time.time() - started_at,
                infra_failure=is_infra,
                error_excerpt=excerpt or None,
            )
            if is_infra:
                terminal_infra_failure = True
                break

        # 6. Terminal failure path — either every attempt was exhausted
        # without passing FTs, or an infra failure tripped the fail-fast
        # break. Persist codegen.json + experiment summary entry, append a
        # skip log line, and rename the iteration folder so iteration-001
        # sees ``iteration-000-baseline-code-failed`` and the sample skips.
        attempts_used = next_attempt_index(
            iteration_code_attempts_dir(iteration_path)
        ) - 1
        terminal_status = "infra_failed" if terminal_infra_failure else "failed"
        _write_codegen_meta(
            iteration_path,
            status=terminal_status,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            task=task,
            winning_attempt=None,
            error=last_error,
            infra_failure=terminal_infra_failure,
        )
        try:
            from ..experiment_summary import append_baseline_codegen_block

            append_baseline_codegen_block(
                sample_dir=sample_dir,
                iteration_path=iteration_path,
                task=task,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
                winning_attempt=None,
                status=terminal_status,
                error=last_error,
            )
        except Exception as sum_exc:
            logger.warning(
                "Could not append baseline codegen block to experiment summary: %s",
                sum_exc,
            )

    skip_reason = (
        f"skipped: baseline codegen aborted on infra failure after "
        f"{attempts_used} attempt(s) — fix the host environment and rerun "
        f"(last error: {last_error or 'unknown'})"
        if terminal_infra_failure
        else (
            f"skipped: baseline codegen failed after {max_attempts} attempt(s) "
            f"(last error: {last_error or 'unknown'})"
        )
    )
    append_k8s_skip(save_dir, sample, skip_reason)
    try:
        mark_iteration_folder_failed(iteration_path)
    except FileExistsError:
        # ``iteration-000-baseline-code-failed`` already exists from a prior
        # run; leave it alone — the skip log captures the latest outcome.
        pass
    return None

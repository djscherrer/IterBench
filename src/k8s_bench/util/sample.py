"""Sample-level gates and helpers for K8s bench modes."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import docker

from ..workspace import image_id_from_test_log


def append_k8s_skip(save_dir: Path, sample: int, reason: str) -> None:
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        p = save_dir / "k8s_bench_skips.log"
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        p.write_text(
            (p.read_text(encoding="utf-8") if p.exists() else "")
            + f"[{ts}] sample{sample}: {reason}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def count_functional_tests(test_results_json: Path) -> tuple[int, int] | None:
    """Return ``(passed, total)`` from ``test_results.json`` or ``None`` if unreadable."""
    if not test_results_json.is_file():
        return None
    try:
        data = json.loads(test_results_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return int(data.get("num_passed_ft", 0)), int(data.get("num_total_ft", 0))


def functional_tests_passed_at(test_results_json: Path) -> bool:
    counts = count_functional_tests(test_results_json)
    if counts is None:
        return False
    passed, total = counts
    return total > 0 and passed >= total


def functional_tests_gate(
    task: Any,
    results_dir: Path,
    sample: int,
) -> bool:
    test_result_path = task.get_test_results_json_path(results_dir, sample)
    save_dir = task.get_save_dir(results_dir)
    counts = count_functional_tests(test_result_path)
    if counts is None:
        reason = (
            "skipped: missing functional test results (functional_tests/test_results.json)"
            if not test_result_path.is_file()
            else "skipped: unreadable functional test results"
        )
        append_k8s_skip(save_dir, sample, reason)
        return False
    passed, total = counts
    if passed < total:
        append_k8s_skip(
            save_dir,
            sample,
            f"skipped: functional tests not all passing ({passed}/{total})",
        )
        return False
    return True


def resolve_image_id_from_test_log(task: Any, results_dir: Path, sample: int) -> str | None:
    test_log = task.get_functional_tests_dir(results_dir, sample) / "test.log"
    return image_id_from_test_log(test_log)


def ensure_docker_image(
    task: Any,
    results_dir: Path,
    sample: int,
    image_id: str | None,
    logger: logging.Logger,
    *,
    code_dir: Path | None = None,
) -> str | None:
    """
    Resolve a Docker image id for bench/deploy.

    Reuses ``image_id`` when it is still present locally **unless** ``code_dir``
    points at an iteration-local snapshot (``iterations/.../02-code/code/``),
    which differs from the sample-level ``code/`` baseline. That case always
    triggers a fresh build so hand-edited or LLM-refined code is picked up.
    """
    sample_code_dir = task.get_code_dir(results_dir, sample)
    iteration_snapshot = (
        code_dir is not None
        and code_dir.is_dir()
        and code_dir.resolve() != sample_code_dir.resolve()
    )
    if iteration_snapshot:
        logger.info(
            "Building image from iteration code snapshot (not sample baseline): %s",
            code_dir,
        )
        return task._build_image_from_code_dir(code_dir, logger)

    if image_id:
        try:
            docker.from_env().images.get(image_id)
            return image_id
        except Exception:
            logger.warning(
                "Image %s found in logs but not in Docker. Rebuilding...",
                image_id,
            )

    if code_dir is not None and code_dir.is_dir():
        return task._build_image_from_code_dir(code_dir, logger)
    return task._build_image(results_dir, sample, logger)


def resolve_locustfile(task: Any, run_dir: Path) -> Path | None:
    from locust_bench.paths import locust_dir
    from scenario_files import SCENARIO_FILE_PATH

    shared = SCENARIO_FILE_PATH.joinpath(f"locustfiles/{task.scenario.id.lower()}.py")
    if task.scenario.locustfile:
        locustfile = locust_dir(run_dir) / f"locustfile-{task.scenario.id.lower()}.py"
        locustfile.write_text(task.scenario.locustfile, encoding="utf-8")
        return locustfile
    if shared.is_file():
        return shared
    return None


def performance_test_names(task: Any) -> list[str]:
    if task.scenario.performance_tests:
        return list(task.scenario.performance_tests)
    from scenario_files import SCENARIO_FILE_PATH

    shared = SCENARIO_FILE_PATH.joinpath(f"locustfiles/{task.scenario.id.lower()}.py")
    if shared.is_file() or task.scenario.locustfile:
        return ["default"]
    return []


def bench_labels(
    task: Any, *, iteration_index: int | None = None
) -> dict[str, str]:
    from tasks import esc

    labels = {
        "baxbench.dev/model": esc(task.model),
        "baxbench.dev/scenario": esc(task.scenario.id),
        "baxbench.dev/env": esc(task.env.id),
    }
    if iteration_index is not None:
        # Kept as ``baxbench.dev/phase`` for back-compat with existing kubectl
        # filters and dashboards; semantically this is the iteration index.
        labels["baxbench.dev/phase"] = str(iteration_index)
    return labels

"""Sample-level gates and helpers for K8s bench modes."""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any

import docker


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


def functional_tests_gate(
    task: Any,
    results_dir: Path,
    sample: int,
) -> bool:
    test_result_path = task.get_test_results_json_path(results_dir, sample)
    save_dir = task.get_save_dir(results_dir)
    if not test_result_path.is_file():
        append_k8s_skip(
            save_dir,
            sample,
            "skipped: missing functional test results (functional_tests/test_results.json)",
        )
        return False
    try:
        data = json.loads(test_result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        append_k8s_skip(save_dir, sample, "skipped: unreadable functional test results")
        return False
    passed = int(data.get("num_passed_ft", 0))
    total = int(data.get("num_total_ft", 0))
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
    pattern = re.compile(r"sha256:[0-9a-f]{64}")
    try:
        for line in test_log.read_text(encoding="utf-8").splitlines():
            match = pattern.search(line)
            if match:
                return match.group(0)
    except OSError:
        pass
    return None


def ensure_docker_image(
    task: Any,
    results_dir: Path,
    sample: int,
    image_id: str | None,
    logger: logging.Logger,
) -> str | None:
    if image_id:
        try:
            docker.from_env().images.get(image_id)
            return image_id
        except Exception:
            logger.warning(
                "Image %s found in logs but not in Docker. Rebuilding...",
                image_id,
            )
    logger.info("Image not found or missing. Building...")
    return task._build_image(results_dir, sample, logger)


def resolve_locustfile(task: Any, run_dir: Path) -> Path | None:
    from scenario_files import SCENARIO_FILE_PATH

    shared = SCENARIO_FILE_PATH.joinpath(f"locustfiles/{task.scenario.id.lower()}.py")
    if task.scenario.locustfile:
        locustfile = run_dir / f"locustfile-{task.scenario.id.lower()}.py"
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


def bench_labels(task: Any, *, phase_index: int | None = None) -> dict[str, str]:
    from tasks import esc

    labels = {
        "baxbench.dev/model": esc(task.model),
        "baxbench.dev/scenario": esc(task.scenario.id),
        "baxbench.dev/env": esc(task.env.id),
    }
    if phase_index is not None:
        labels["baxbench.dev/phase"] = str(phase_index)
    return labels

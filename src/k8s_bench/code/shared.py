"""Shared docker, filesystem, and functional-test helpers for code generation."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

import docker
import shutil


def read_log_tail(path: Path, *, max_chars: int = 32_000) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def ensure_docker_network() -> None:
    client = docker.from_env()
    if not [n for n in client.networks.list() if n.name == "baxbench-net"]:
        client.networks.create(name="baxbench-net", driver="bridge")


def write_code_files(files: dict[Path, str], code_dir: Path) -> None:
    if code_dir.is_dir():
        shutil.rmtree(code_dir)
    code_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        dest = code_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def run_functional_tests(
    task: Any,
    *,
    code_dir: Path,
    ft_dir: Path,
    ft_timeout: int,
    num_ports: int,
    min_port: int,
) -> bool:
    from tasks import SlotManager

    ensure_docker_network()
    with multiprocessing.Manager() as manager:
        port_manager = SlotManager(manager, num_ports, min_port)
        return task.test_functional_tests_at(
            code_dir=code_dir,
            ft_dir=ft_dir,
            port_manager=port_manager,
            timeout=ft_timeout,
        )


def ft_pass_counts(ft_dir: Path) -> tuple[int, int]:
    counts = ft_counts_from_json(ft_dir / "test_results.json")
    if counts is None:
        return 0, 0
    return counts


def ft_counts_from_json(test_results_json: Path) -> tuple[int, int] | None:
    """Return ``(passed, total)`` from ``test_results.json`` or ``None`` if unreadable."""
    if not test_results_json.is_file():
        return None
    try:
        data = json.loads(test_results_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return int(data.get("num_passed_ft", 0)), int(data.get("num_total_ft", 0))


def functional_tests_passed_at(test_results_json: Path) -> bool:
    counts = ft_counts_from_json(test_results_json)
    if counts is None:
        return False
    passed, total = counts
    return total > 0 and passed >= total

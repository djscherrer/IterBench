"""Path helpers and file persistence for the per-scenario artifact layout
scenario_builder produces — a different domain from the rest of workspace/
(k8s-bench's iteration-directory layout), but the same kind of thing: where
does this pipeline's state live on disk.

Layout under a scenario root (``{args.path}/{scenario}/``)::

    spec/scenario.json                 canonical scenario definition
    spec/tasklist.json
    snapshots/functional/iu{n}.json    scenario state after functional iteration n
    snapshots/security/iw{n}.json      scenario state after security iteration n
    snapshots/performance/ip{n}.json   scenario state after performance iteration n
    snapshots/performance/ip{n}_locustfile.py
    implementations/{tag}{n}.json      tag in {it, iu, iw}: generated solution code
    results/{tag}{n}/summary.json      status-only test outcomes (no logs)
    results/{tag}{n}/summary.png       heatmap visualization
    exports/{tag}{n}.py                BaxBench-ready exported modules
    logs/{tag}{n}/{test_name}/{model}/container.log
    logs/{tag}{n}/{test_name}/{model}/test.log
    logs/llm_cost_ledger.json          per-stage LLM token/cost breakdown (see token_usage.py)
    logs/verdicts.txt
    failures/functional/<loop><n>-<implementation>.json
    failures/performance/locust-<attempt>.json
    conversations/implementations/<implementation>.json
    conversations/functional/test_suite.json
    conversations/performance/locust.json
    conversations/scenario_generation/idea_author.json
    conversations/scenario_generation/spec_author.json

``conversations/implementations/`` is phase-agnostic on purpose: the same
implementation-owner thread is repaired across the functional and exploit
phases (and potentially performance), so it lives at the scenario root
rather than under any one phase's folder.

Before a scenario title has been accepted, the candidate itself remains in
memory. Its author conversations and confirmed generation-failure diagnostics
are persisted under ``{args.path}/.scenario_builder/generation_runs/<run-id>/``.
Once accepted, the author conversations are copied into the scenario artifact
directory alongside the canonical scenario definition.

All functions take ``root`` (the scenario folder, i.e. ``scenario_folder_path``)
as their first argument so callers stay explicit about which scenario they mean.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import pathlib
from datetime import datetime

_SNAPSHOT_KIND = {"iu": "functional", "iw": "security", "ip": "performance"}


def ensure_scenario_dirs(root: str) -> None:
    """Create the full subdirectory tree for a scenario root."""
    os.makedirs(os.path.join(root, "spec"), exist_ok=True)
    os.makedirs(os.path.join(root, "implementations"), exist_ok=True)
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    os.makedirs(os.path.join(root, "exports"), exist_ok=True)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(root, "failures", "functional"), exist_ok=True)
    os.makedirs(os.path.join(root, "failures", "performance"), exist_ok=True)
    os.makedirs(os.path.join(root, "conversations", "functional"), exist_ok=True)
    os.makedirs(os.path.join(root, "conversations", "implementations"), exist_ok=True)
    os.makedirs(os.path.join(root, "conversations", "performance"), exist_ok=True)
    os.makedirs(
        os.path.join(root, "conversations", "scenario_generation"), exist_ok=True
    )
    for kind in _SNAPSHOT_KIND.values():
        os.makedirs(os.path.join(root, "snapshots", kind), exist_ok=True)


def spec_path(root: str) -> str:
    return os.path.join(root, "spec", "scenario.json")


def tasklist_path(root: str) -> str:
    return os.path.join(root, "spec", "tasklist.json")


def snapshot_path(root: str, tag: str, n) -> str:
    """tag in {iu, iw, ip}. The un-tagged base spec is `spec_path`."""
    return os.path.join(root, "snapshots", _SNAPSHOT_KIND[tag], f"{tag}{n}.json")


def snapshot_glob(root: str, tag: str) -> list[str]:
    return glob.glob(
        os.path.join(root, "snapshots", _SNAPSHOT_KIND[tag], f"{tag}*.json")
    )


def locustfile_snapshot_path(root: str, n) -> str:
    return os.path.join(root, "snapshots", "performance", f"ip{n}_locustfile.py")


def latest_index(root: str, tag: str) -> int | None:
    """Highest N for which snapshots/{kind}/{tag}N.json exists, else None."""
    best: int | None = None
    for path in snapshot_glob(root, tag):
        middle = os.path.basename(path)[len(tag) : -len(".json")]
        if middle.isdigit():
            n = int(middle)
            if best is None or n > best:
                best = n
    return best


def latest_scenario_snapshot_path(root: str) -> str:
    """Latest functional (iu) snapshot if one exists, else the base spec."""
    iu_latest = latest_index(root, "iu")
    if iu_latest is not None:
        return snapshot_path(root, "iu", iu_latest)
    return spec_path(root)


def implementation_path(root: str, tag: str, n) -> str:
    return os.path.join(root, "implementations", f"{tag}{n}.json")


def implementation_glob(root: str, tag: str | None = None) -> list[str]:
    pattern = f"{tag}*.json" if tag else "*.json"
    return glob.glob(os.path.join(root, "implementations", pattern))


def results_dir(root: str, tag: str, n) -> str:
    return os.path.join(root, "results", f"{tag}{n}")


def results_summary_path(root: str, tag: str, n) -> str:
    return os.path.join(results_dir(root, tag, n), "summary.json")


def results_png_path(root: str, tag: str, n) -> str:
    return os.path.join(results_dir(root, tag, n), "summary.png")


def export_path(
    root: str, tag: str | None = None, n=None, filename: str | None = None
) -> str:
    if filename is not None:
        return os.path.join(root, "exports", filename)
    return os.path.join(root, "exports", f"{tag}{n}.py")


def _sanitize_model_key(key: str) -> str:
    return key.replace(" ", "_").replace("/", "_")


def log_dir(root: str, tag: str, n, test_name: str, model_key: str) -> str:
    return os.path.join(
        root, "logs", f"{tag}{n}", test_name, _sanitize_model_key(model_key)
    )


def llm_cost_ledger_path(root: str) -> str:
    return os.path.join(root, "logs", "llm_cost_ledger.json")


def verdicts_path(root: str) -> str:
    return os.path.join(root, "logs", "verdicts.txt")


def _artifact_filename(value: str) -> str:
    """Filesystem-safe, readable name for a builder artifact identifier."""
    readable = (
        "".join(
            char if char.isalnum() or char in "._-" else "_" for char in value
        ).strip("._")
        or "artifact"
    )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def functional_failure_path(
    root: str, loop: str, iteration: int, implementation_key: str
) -> str:
    """Path for one builder functional-failure record.

    ``loop`` is deliberately part of the filename: the same implementation can
    receive provisional black-box feedback and later specification-validated
    white-box feedback during the same numeric iteration.
    """
    return os.path.join(
        root,
        "failures",
        "functional",
        f"{loop}{iteration}-{_artifact_filename(implementation_key)}.json",
    )


def implementation_conversation_path(root: str, implementation_key: str) -> str:
    """Persisted repair thread for one generated implementation.

    Phase-agnostic: the same implementation-owner thread is continued across
    the functional and exploit phases (and potentially performance), so it
    lives directly under ``conversations/`` rather than under one phase's
    subfolder.
    """
    return os.path.join(
        root,
        "conversations",
        "implementations",
        f"{_artifact_filename(implementation_key)}.json",
    )


def legacy_functional_implementation_conversation_path(
    root: str, implementation_key: str
) -> str:
    """Pre-migration location (nested under the functional phase), retained
    for read migration only."""
    return os.path.join(
        root,
        "conversations",
        "functional",
        "implementations",
        f"{_artifact_filename(implementation_key)}.json",
    )


def functional_test_suite_conversation_path(root: str) -> str:
    """Durable author conversation for one scenario's functional test suite."""
    return os.path.join(root, "conversations", "functional", "test_suite.json")


def locust_failure_path(root: str, attempt: int, kind: str) -> str:
    """Path for a Locust generation/verification failure record."""
    return os.path.join(
        root,
        "failures",
        "performance",
        f"locust-{attempt}-{_artifact_filename(kind)}.json",
    )


def locust_candidate_path(root: str, attempt: int) -> str:
    """Preserve an invalid Locust candidate without marking it scenario-valid."""
    return os.path.join(root, "failures", "performance", f"locust-{attempt}.py")


def locust_conversation_path(root: str) -> str:
    """Durable Locust-author conversation for one scenario."""
    return os.path.join(root, "conversations", "performance", "locust.json")


def legacy_lowcost_conversation_path(root: str) -> str:
    """Short-lived alternate filename, retained for read migration."""
    return os.path.join(root, "conversations", "performance", "lowcost.json")


def generation_run_dir(root: str, run_id: str) -> str:
    """Artifact root for scenario generation before a scenario title exists."""
    return os.path.join(root, ".scenario_builder", "generation_runs", run_id)


def generation_failure_path(
    root: str, run_id: str, stage: str, attempt: int, kind: str
) -> str:
    """Path for one scenario-generation failure record."""
    return os.path.join(
        generation_run_dir(root, run_id),
        "failures",
        f"{stage}-{attempt}-{_artifact_filename(kind)}.json",
    )


def generation_conversation_path(root: str, run_id: str, author: str) -> str:
    """Durable pre-title conversation for one scenario-generation author."""
    return os.path.join(
        generation_run_dir(root, run_id), "conversations", f"{author}.json"
    )


def scenario_generation_conversation_path(root: str, author: str) -> str:
    """Accepted scenario's copy of one scenario-generation author history."""
    return os.path.join(root, "conversations", "scenario_generation", f"{author}.json")


def write_results(root: str, tag: str, n, full_results: dict) -> None:
    """Persist test results as a lean status-only summary.json, with any
    container/test logs split out into sidecar files under logs/."""
    summary: dict = {}
    for test_name, per_model in full_results.items():
        if not isinstance(per_model, dict):
            summary[test_name] = per_model
            continue
        summary[test_name] = {}
        for model_key, result in per_model.items():
            if not isinstance(result, dict):
                summary[test_name][model_key] = result
                continue
            summary[test_name][model_key] = {
                k: v
                for k, v in result.items()
                if k not in ("container_logs", "test_logs")
            }
            container_logs = result.get("container_logs") or ""
            test_logs = result.get("test_logs") or ""
            if container_logs or test_logs:
                d = log_dir(root, tag, n, test_name, model_key)
                os.makedirs(d, exist_ok=True)
                if container_logs:
                    with open(os.path.join(d, "container.log"), "w") as f:
                        f.write(container_logs)
                if test_logs:
                    with open(os.path.join(d, "test.log"), "w") as f:
                        f.write(test_logs)

    os.makedirs(results_dir(root, tag, n), exist_ok=True)
    with open(results_summary_path(root, tag, n), "w") as f:
        json.dump(summary, f, indent=4)


def read_results(root: str, tag: str, n, with_logs: bool = True) -> dict:
    """Load a summary.json, reattaching container/test logs from their
    sidecar files (unless with_logs=False, e.g. for lightweight status checks)."""
    with open(results_summary_path(root, tag, n)) as f:
        summary = json.load(f)

    if not with_logs:
        return summary

    full: dict = {}
    for test_name, per_model in summary.items():
        if not isinstance(per_model, dict):
            full[test_name] = per_model
            continue
        full[test_name] = {}
        for model_key, result in per_model.items():
            if not isinstance(result, dict):
                full[test_name][model_key] = result
                continue
            entry = dict(result)
            d = log_dir(root, tag, n, test_name, model_key)
            c_path = os.path.join(d, "container.log")
            t_path = os.path.join(d, "test.log")
            entry["container_logs"] = (
                open(c_path).read() if os.path.exists(c_path) else ""
            )
            entry["test_logs"] = open(t_path).read() if os.path.exists(t_path) else ""
            full[test_name][model_key] = entry
    return full


def load_code(code_dir: str) -> dict[pathlib.Path, str]:
    """Load all code files from a directory into a dict of relative path -> content."""
    code_dir_path = pathlib.Path(code_dir)
    files: dict[pathlib.Path, str] = {}
    for root_dir, _, file_names in os.walk(code_dir_path):
        for file in file_names:
            abs_path = pathlib.Path(root_dir) / file
            with open(abs_path, "r") as f:
                content = f.read()
            files[abs_path.relative_to(code_dir_path)] = content
    return files


def save_code(files: dict[pathlib.Path, str], code_dir: str) -> None:
    """Save a dict of relative path -> content into a directory."""
    code_dir_path = pathlib.Path(code_dir)
    code_dir_path.mkdir(parents=True, exist_ok=True)
    for path, code in files.items():
        full_path = code_dir_path / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with open(full_path, "w") as f:
            f.write(code)


def record_verdict(root: str, message: str, verdict: str) -> None:
    """Append a verdict message to this scenario's verdicts log file."""
    path = verdicts_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message=} {verdict=}\n"
        )

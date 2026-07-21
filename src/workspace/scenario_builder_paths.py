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

All functions take ``root`` (the scenario folder, i.e. ``scenario_folder_path``)
as their first argument so callers stay explicit about which scenario they mean.
"""

from __future__ import annotations

import glob
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


def export_path(root: str, tag: str | None = None, n=None, filename: str | None = None) -> str:
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
                k: v for k, v in result.items() if k not in ("container_logs", "test_logs")
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
            entry["container_logs"] = open(c_path).read() if os.path.exists(c_path) else ""
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
    with open(verdicts_path(root), "a") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message=} {verdict=}\n"
        )

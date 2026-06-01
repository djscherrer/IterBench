"""Per-iteration ``meta.json`` helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .layout import ensure_iteration_core_layout
from .paths import iteration_meta_path, normalize_iteration_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_iteration_meta(iteration_path: Path) -> dict[str, Any]:
    path = iteration_meta_path(iteration_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_iteration_meta(iteration_path: Path, meta: dict[str, Any]) -> Path:
    ensure_iteration_core_layout(iteration_path)
    path = iteration_meta_path(iteration_path)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def init_iteration_meta(
    iteration_path: Path,
    *,
    iteration_index: int,
    iteration_id: str,
    based_on_iteration: str | None = None,
) -> dict[str, Any]:
    iid = normalize_iteration_id(iteration_id)
    meta = {
        "iteration_index": iteration_index,
        "iteration_id": iid,
        "folder": iteration_path.name,
        "based_on_iteration": based_on_iteration,
        "refinement_action": None,
        "refinement_mode": None,
        "code_modified": False,
        "spec_regenerated": False,
        "spec_reused_from": None,
        "status": "pending",
        "failure_reason": None,
        "started_at": _utc_now(),
        "finished_at": None,
    }
    write_iteration_meta(iteration_path, meta)
    return meta


def update_iteration_meta(iteration_path: Path, **fields: Any) -> dict[str, Any]:
    meta = read_iteration_meta(iteration_path)
    meta.update(fields)
    write_iteration_meta(iteration_path, meta)
    return meta


def iteration_is_failed(iteration_path: Path) -> bool:
    """True when the folder is suffixed ``-failed`` or ``meta.json`` says failed."""
    from .paths import iteration_folder_is_failed

    if iteration_folder_is_failed(iteration_path.name):
        return True
    return read_iteration_meta(iteration_path).get("status") == "failed"

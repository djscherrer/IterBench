"""
Machine-readable manifest for bulk reverification runs.

One JSON file at the results-tree root (``reverification_manifest.json`` by
default) doubles as the discovery report and the idempotency ledger: an
iteration recorded here with ``status == "success"`` for the requested load
profile is skipped on a later run unless ``--force`` is given. Keying by
load profile (rather than "any complete bench run") is deliberate: switching
``--load-profile`` is a materially different request and must never silently
reuse a run made under a different profile.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Status = Literal["success", "skipped", "failed"]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_key(
    *, sample_dir: Path, results_root: Path, experiment_id: str, iteration_id: str
) -> str:
    """
    Stable identity for one logical iteration, independent of folder-name suffix.

    Keyed on the sample directory (relative to the results root) plus
    experiment id and canonical iteration id, so a folder rename performed by
    ``fail_iteration_phase`` (``iteration-003-code`` -> ``iteration-003-code-failed``)
    does not orphan that iteration's manifest history on the next run.
    """
    try:
        rel_sample = sample_dir.resolve().relative_to(results_root.resolve()).as_posix()
    except ValueError:
        rel_sample = sample_dir.as_posix()
    return f"{rel_sample}::{experiment_id}::{iteration_id}"


def path_key(path: Path, *, results_root: Path) -> str:
    """Fallback manifest key for entries with no resolvable task metadata."""
    try:
        return path.resolve().relative_to(results_root.resolve()).as_posix()
    except ValueError:
        return str(path)


@dataclass
class ManifestEntry:
    key: str
    status: Status
    reason: str | None
    original_path: str
    task: dict[str, Any]
    iteration_id: str
    load_profile: str
    timestamp: str
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def already_reverified(entry: ManifestEntry | None, *, load_profile: str) -> bool:
    """True when ``entry`` records a successful reverification with this exact load profile."""
    return entry is not None and entry.status == "success" and entry.load_profile == load_profile


def load_manifest(path: Path) -> dict[str, ManifestEntry]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries_raw = raw.get("iterations", {}) if isinstance(raw, dict) else {}
    entries: dict[str, ManifestEntry] = {}
    if not isinstance(entries_raw, dict):
        return entries
    for key, data in entries_raw.items():
        if not isinstance(data, dict) or "status" not in data:
            continue
        entries[key] = ManifestEntry(
            key=key,
            status=data["status"],
            reason=data.get("reason"),
            original_path=data.get("original_path", ""),
            task=data.get("task", {}) or {},
            iteration_id=data.get("iteration_id", ""),
            load_profile=data.get("load_profile", ""),
            timestamp=data.get("timestamp", ""),
            artifacts=data.get("artifacts", {}) or {},
        )
    return entries


def write_manifest(
    path: Path,
    entries: dict[str, ManifestEntry],
    *,
    results_root: Path,
    cluster: str,
    load_profile: str,
) -> None:
    payload = {
        "generated_at": utc_now(),
        "results_root": str(results_root),
        "cluster": cluster,
        "load_profile": load_profile,
        "iterations": {key: entry.to_dict() for key, entry in sorted(entries.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ManifestStore:
    """Thread-safe in-memory manifest, flushed to disk with :func:`write_manifest`."""

    def __init__(self, entries: dict[str, ManifestEntry] | None = None) -> None:
        self._entries: dict[str, ManifestEntry] = dict(entries or {})
        self._lock = threading.Lock()

    def get(self, key: str) -> ManifestEntry | None:
        with self._lock:
            return self._entries.get(key)

    def set(self, key: str, entry: ManifestEntry) -> None:
        with self._lock:
            self._entries[key] = entry

    def setdefault_skip(self, key: str, entry: ManifestEntry) -> None:
        """Set only if absent — used for discovery-time skips that must not
        clobber a richer entry written by the same run."""
        with self._lock:
            self._entries.setdefault(key, entry)

    def snapshot(self) -> dict[str, ManifestEntry]:
        with self._lock:
            return dict(self._entries)

    def counts(self) -> dict[str, int]:
        with self._lock:
            out = {"success": 0, "failed": 0, "skipped": 0}
            for entry in self._entries.values():
                out[entry.status] = out.get(entry.status, 0) + 1
            return out


__all__ = [
    "Status",
    "ManifestEntry",
    "ManifestStore",
    "already_reverified",
    "load_manifest",
    "write_manifest",
    "manifest_key",
    "path_key",
    "utc_now",
]

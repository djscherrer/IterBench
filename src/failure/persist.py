"""Generic JSON persistence for :class:`failure.FailureRecord`."""

from __future__ import annotations

import json
from pathlib import Path

from .record import FailureRecord


def persist_failure_record(path: str | Path, record: FailureRecord) -> Path:
    """Write one record atomically enough for scenario-builder retry artifacts."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output

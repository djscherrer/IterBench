"""Append human-readable skip reasons for k8s-bench sample runs."""

from __future__ import annotations

import datetime
from pathlib import Path


def append_k8s_skip(task_run_dir: Path, sample: int, reason: str) -> None:
    try:
        task_run_dir.mkdir(parents=True, exist_ok=True)
        p = task_run_dir / "k8s_bench_skips.log"
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        p.write_text(
            (p.read_text(encoding="utf-8") if p.exists() else "")
            + f"[{ts}] sample{sample}: {reason}\n",
            encoding="utf-8",
        )
    except OSError:
        pass

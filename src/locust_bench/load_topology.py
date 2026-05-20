"""SSH load-generator topology (master + workers)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def _dedupe_hosts(hosts: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in hosts:
        h = (raw or "").strip()
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return tuple(out)


@dataclass(frozen=True)
class LoadTopology:
    """Locust distributed mode: one master host and zero or more worker hosts."""

    master: str
    workers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.master).strip():
            raise ValueError("LoadTopology requires a non-empty master host")

    @property
    def all_hosts(self) -> tuple[str, ...]:
        return _dedupe_hosts((self.master, *self.workers))

    @classmethod
    def from_profile_fields(
        cls,
        *,
        load_master: str,
        load_workers: Sequence[str] = (),
    ) -> LoadTopology:
        return cls(master=str(load_master).strip(), workers=tuple(load_workers))

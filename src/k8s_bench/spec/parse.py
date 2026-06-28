"""Parse LLM spec fragments into :class:`~k8s_bench.spec.models.K8sWorkloadSpec`."""

from __future__ import annotations

import re
from typing import Any

import yaml

from ..workspace import default_k8s_namespace, normalize_iteration_id
from .components import DEFAULT_READ_POOLER_SERVICE, CacheSpec, PoolerSpec
from .models import BackendSpec, DatabaseSpec, K8sWorkloadSpec

_IMAGE_PLACEHOLDER = "baxbench/pending-at-bench:latest"

_SPEC_BLOCK_RE = re.compile(r"<SPEC>\s*(.*?)\s*</SPEC>", re.DOTALL | re.IGNORECASE)
_YAML_FENCE_RE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_spec_fragment(response: str) -> dict[str, Any]:
    text = match.group(1).strip() if match else ""
    if not text:
        fences = _YAML_FENCE_RE.findall(response)
        if fences:
            text = fences[-1].strip()
    if not text:
        raise ValueError("Model response did not contain <SPEC> YAML or a ```yaml``` block")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Parsed spec fragment is not a YAML mapping")
    return data


def _parse_backend_placement(backend_raw: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    placement_raw = backend_raw.get("placement") or {}
    workers: list[str] = []
    spread = True
    if isinstance(placement_raw, dict):
        raw_workers = placement_raw.get("workers") or placement_raw.get("worker_nodes") or []
        if isinstance(raw_workers, (list, tuple)):
            workers = [str(w).strip() for w in raw_workers if str(w).strip()]
        spread = bool(placement_raw.get("spread_replicas", True))
    return tuple(workers), spread


def merge_fragment_into_spec(
    fragment: dict[str, Any],
    *,
    iteration_id: str,
    app_port: int,
    needs_db: bool,
    labels: dict[str, str],
) -> K8sWorkloadSpec:
    iid = normalize_iteration_id(iteration_id)
    backend_raw = fragment.get("backend") or {}
    if not isinstance(backend_raw, dict):
        raise ValueError("spec fragment must include backend mapping")

    db_raw = fragment.get("database") or {}
    if not isinstance(db_raw, dict):
        db_raw = {}

    placement_workers, spread_replicas = _parse_backend_placement(backend_raw)
    backend = BackendSpec.from_mapping(
        {
            **backend_raw,
            "image": backend_raw.get("image") or _IMAGE_PLACEHOLDER,
            "port": backend_raw.get("port") or app_port,
            "placement": {
                **(
                    backend_raw.get("placement")
                    if isinstance(backend_raw.get("placement"), dict)
                    else {}
                ),
                "workers": list(placement_workers),
                "spread_replicas": spread_replicas,
            },
        }
    )
    pooler_raw = fragment.get("pooler")
    pooler = PoolerSpec.from_mapping(
        pooler_raw if isinstance(pooler_raw, dict) else None
    )
    read_pooler_raw = fragment.get("read_pooler")
    read_pooler = (
        PoolerSpec.from_mapping(
            read_pooler_raw, default_service_name=DEFAULT_READ_POOLER_SERVICE
        )
        if isinstance(read_pooler_raw, dict)
        else PoolerSpec(enabled=False, service_name=DEFAULT_READ_POOLER_SERVICE)
    )
    cache_raw = fragment.get("cache")
    cache = (
        CacheSpec.from_mapping(cache_raw)
        if isinstance(cache_raw, dict)
        else CacheSpec()
    )
    database = DatabaseSpec.from_mapping(
        {
            "enabled": needs_db if needs_db else bool(db_raw.get("enabled", True)),
            **db_raw,
        }
        if needs_db
        else {"enabled": False}
    )
    return K8sWorkloadSpec(
        iteration_id=iid,
        namespace=default_k8s_namespace(iid),
        backend=backend,
        database=database,
        pooler=pooler,
        read_pooler=read_pooler,
        cache=cache,
        labels=dict(labels),
    )

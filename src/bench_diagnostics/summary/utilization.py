"""Aggregate ``kubectl top`` CSV samples."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from ..paths import resolve_kubernetes_metrics_cluster_dir
from ._stats import distribution_float, distribution_int


def _parse_cpu_millicores(value: str) -> int | None:
    v = (value or "").strip()
    if not v or v == "<unknown>":
        return None
    if v.endswith("m"):
        return int(round(float(v[:-1])))
    try:
        return int(round(float(v) * 1000))
    except ValueError:
        return None


def _parse_memory_mi(value: str) -> int | None:
    v = (value or "").strip()
    if not v or v == "<unknown>":
        return None
    if v.endswith("Mi"):
        return int(round(float(v[:-2])))
    if v.endswith("Gi"):
        return int(round(float(v[:-2]) * 1024))
    if v.endswith("Ki"):
        return int(round(float(v[:-2]) / 1024))
    return None


def _parse_pct(value: str) -> float | None:
    v = (value or "").strip().rstrip("%")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _pod_tier(pod: str) -> str:
    name = (pod or "").lower()
    if name.startswith("backend"):
        return "backend"
    if "replica" in name and "postgres" in name:
        return "db-replica"
    if name.startswith("postgres"):
        return "db-primary"
    if "pgbouncer-read" in name or name.startswith("pgbouncer-read"):
        return "read-pooler"
    if name.startswith("pgbouncer"):
        return "pooler"
    if name.startswith("redis-db"):
        return "db-cache"
    if name.startswith("redis"):
        return "cache"
    return "other"


def _summarize_pod_top_by_tier(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    cpu_by_tier: dict[str, list[int]] = defaultdict(list)
    mem_by_tier: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        pod = (row.get("pod") or "").strip()
        if not pod:
            continue
        tier = _pod_tier(pod)
        cpu = _parse_cpu_millicores(row.get("cpu") or "")
        mem = _parse_memory_mi(row.get("memory") or "")
        if cpu is not None:
            cpu_by_tier[tier].append(cpu)
        if mem is not None:
            mem_by_tier[tier].append(mem)

    tiers = sorted(cpu_by_tier.keys() | mem_by_tier.keys())
    if not tiers:
        return ""

    lines = [
        f"kubectl top by tier: {samples} sample(s)",
        "",
        "| Tier | CPU m (min/p50/avg/p95/max) | Memory Mi (min/p50/avg/p95/max) |",
        "|---|---:|---:|",
    ]
    for tier in tiers:
        lines.append(
            f"| {tier} | {distribution_int(cpu_by_tier[tier])} | "
            f"{distribution_int(mem_by_tier[tier])} |"
        )
    return "\n".join(lines)


def _summarize_pod_top_csv(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    cpu_by_pod: dict[str, list[int]] = defaultdict(list)
    mem_by_pod: dict[str, list[int]] = defaultdict(list)

    for row in rows:
        pod = (row.get("pod") or "").strip()
        if not pod:
            continue
        cpu = _parse_cpu_millicores(row.get("cpu") or "")
        mem = _parse_memory_mi(row.get("memory") or "")
        if cpu is not None:
            cpu_by_pod[pod].append(cpu)
        if mem is not None:
            mem_by_pod[pod].append(mem)

    pod_names = sorted(cpu_by_pod.keys() | mem_by_pod.keys())
    lines = [
        f"kubectl top pods: {samples} sample(s), {len(pod_names)} pod(s)",
        "",
        "| Pod | CPU m (min/p50/avg/p95/max) | Memory Mi (min/p50/avg/p95/max) |",
        "|---|---:|---:|",
    ]
    for pod in pod_names:
        lines.append(
            f"| {pod} | {distribution_int(cpu_by_pod[pod])} | "
            f"{distribution_int(mem_by_pod[pod])} |"
        )
    return "\n".join(lines)


def _summarize_node_top_csv(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""

    samples = len({r.get("ts_epoch_s", "") for r in rows})
    cpu_by_node: dict[str, list[int]] = defaultdict(list)
    cpu_pct_by_node: dict[str, list[float]] = defaultdict(list)
    mem_mi_by_node: dict[str, list[int]] = defaultdict(list)
    mem_pct_by_node: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        node = (row.get("node") or "").strip()
        if not node:
            continue
        short = node.split(".", 1)[0]
        cpu = _parse_cpu_millicores(row.get("cpu") or "")
        if cpu is not None:
            cpu_by_node[short].append(cpu)
        pct = _parse_pct(row.get("cpu_pct") or "")
        if pct is not None:
            cpu_pct_by_node[short].append(pct)
        mem = _parse_memory_mi(row.get("memory") or "")
        if mem is not None:
            mem_mi_by_node[short].append(mem)
        mpct = _parse_pct(row.get("memory_pct") or "")
        if mpct is not None:
            mem_pct_by_node[short].append(mpct)

    lines = [
        f"kubectl top nodes: {samples} sample(s)",
        "",
        "| Node | CPU m (min/p50/avg/p95/max) | CPU % (min/p50/avg/p95/max) | "
        "Memory Mi (min/p50/avg/p95/max) | Memory % (min/p50/avg/p95/max) |",
        "|---|---:|---:|---:|---:|",
    ]
    for node in sorted(cpu_by_node.keys()):
        lines.append(
            f"| {node} | {distribution_int(cpu_by_node[node])} | "
            f"{distribution_float(cpu_pct_by_node[node])} | "
            f"{distribution_int(mem_mi_by_node[node])} | "
            f"{distribution_float(mem_pct_by_node[node])} |"
        )
    return "\n".join(lines)


def summarize_k8s_utilization(run_dir: Path) -> str:
    """Aggregate ``diagnostics/kubernetes/metrics/cluster/kubectl_top_*.csv``."""
    cluster_dir = resolve_kubernetes_metrics_cluster_dir(run_dir)
    pod_csv = cluster_dir / "kubectl_top_pods.csv"
    node_csv = cluster_dir / "kubectl_top_nodes.csv"
    parts: list[str] = []

    tier_block = _summarize_pod_top_by_tier(pod_csv)
    if tier_block:
        parts.append("### By tier")
        parts.append(tier_block)

    pod_block = _summarize_pod_top_csv(pod_csv)
    if pod_block:
        parts.append("### Per pod")
        parts.append(pod_block)

    node_block = _summarize_node_top_csv(node_csv)
    if node_block:
        parts.append("### Nodes")
        parts.append(node_block)

    if not parts:
        return "(kubernetes metrics unavailable)"
    return "\n\n".join(parts)

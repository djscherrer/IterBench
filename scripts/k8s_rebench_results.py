#!/usr/bin/env python3
"""
Bulk re-benchmark / reverify existing Kubernetes iteration results.

    cp -r results results_reverified
    python scripts/k8s_rebench_results.py \\
        --results-dir results_reverified \\
        --cluster baxbench-emulab \\
        --load-profile default \\
        --force

This tool NEVER writes outside the directory passed to ``--results-dir``, so
copying ``results/`` first and pointing this at the copy is what keeps the
original untouched — it never reads or infers a "source" directory of its
own.

It recursively walks ``--results-dir`` for
``k8s-experiments/<slug>/iterations/iteration-*`` folders (see
``docs/k8s_approach.md``), derives the originating BaxBench task (model,
scenario, environment, temperature, spec type, safety prompt, sample,
experiment) from each iteration's path, and re-runs ONLY the existing
deploy + Locust bench stages (``k8s_bench.orchestration.deploy_only``)
against the already-generated code/spec on disk. No LLM calls, no code or
spec regeneration, no refinement-decision routing happen in this mode.

See ``docs/k8s_approach.md`` ("Bulk re-benchmarking / reverification") for
the full write-up: dry-run, resuming, forcing, and where reports land.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from env import all_envs  # noqa: E402
from scenarios import all_scenarios  # noqa: E402
from tasks import Task  # noqa: E402
from workspace import normalize_iteration_id, read_iteration_meta  # noqa: E402

from k8s_bench.reverify import (  # noqa: E402
    DiscoveredIteration,
    DiscoveryFilters,
    DiscoveryReport,
    ManifestEntry,
    SkippedIteration,
    already_reverified,
    clear_stale_reverify_artifacts,
    discover_iterations,
    group_by_sample_experiment,
    load_manifest,
    manifest_key,
    path_key,
    stale_artifact_targets,
    utc_now,
    write_manifest,
)
from workspace import iteration_deploy_dir  # noqa: E402
from workspace.paths import iteration_folder_is_failed, parse_iteration_index  # noqa: E402

logger = logging.getLogger("k8s_rebench_results")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--results-dir",
        required=True,
        type=Path,
        help="Root of a COPIED results tree (e.g. results_reverified/). Never the "
        "original results/ — this tool writes freely inside whatever you point it at.",
    )
    p.add_argument("--cluster", required=True, type=str, help="Cluster profile (k8s_bench/cluster/profiles.py).")
    p.add_argument(
        "--load-profile", default="quick-check", type=str, help="Locust load profile for every re-bench."
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Redo iterations already recorded as successfully reverified with this load profile.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List exactly what would be processed/deleted; no cluster contact, no writes.",
    )
    p.add_argument(
        "--include-failed",
        action="store_true",
        help="Also consider '-failed' iteration folders (if they have a spec + resolvable code).",
    )
    p.add_argument("--models", nargs="+", default=None, help="Filter: model directory name(s) as on disk.")
    p.add_argument("--scenarios", nargs="+", default=None, help="Filter: BaxBench scenario id(s).")
    p.add_argument("--envs", nargs="+", default=None, help="Filter: environment id(s), e.g. Go-net/http.")
    p.add_argument("--samples", nargs="+", type=int, default=None, help="Filter: sample number(s).")
    p.add_argument("--experiments", nargs="+", default=None, help="Filter: k8s-experiments slug(s).")
    p.add_argument(
        "--iterations", nargs="+", default=None, help="Filter: iteration id(s), e.g. iteration-003 or 3."
    )
    p.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Max concurrent sample/experiment groups (default: 1, sequential). "
        "Iterations within one group always run sequentially.",
    )
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--k8s-wait-timeout", type=int, default=300)
    p.add_argument("--bench-users", type=int, default=None)
    p.add_argument("--bench-spawn-rate", type=int, default=None)
    p.add_argument("--bench-run-time", type=int, default=None)
    p.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Override manifest location (default: <results-dir>/reverification_manifest.json).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _build_filters(args: argparse.Namespace) -> DiscoveryFilters:
    return DiscoveryFilters(
        models=frozenset(args.models) if args.models else None,
        scenarios=frozenset(args.scenarios) if args.scenarios else None,
        envs=frozenset(args.envs) if args.envs else None,
        samples=frozenset(args.samples) if args.samples else None,
        experiments=frozenset(args.experiments) if args.experiments else None,
        iterations=(
            frozenset(normalize_iteration_id(i) for i in args.iterations)
            if args.iterations
            else None
        ),
        include_failed=args.include_failed,
    )


def _build_task(discovered: DiscoveredIteration) -> Task:
    t = discovered.task
    return Task(
        env=t.env,
        scenario=t.scenario,
        model=t.model,
        temperature=t.temperature,
        reasoning_effort="high",  # unused: deploy-only mode makes no LLM calls
        spec_type=t.spec_type,
        safety_prompt=t.safety_prompt,
        provider=None,  # unused: deploy-only mode makes no LLM calls
        use_stubs=False,
        run_security_tests=False,
    )


def _task_dict(d: DiscoveredIteration) -> dict[str, Any]:
    t = d.task
    return {
        "model": t.model,
        "scenario": t.scenario.id,
        "env": t.env.id,
        "temperature": t.temperature,
        "spec_type": t.spec_type,
        "safety_prompt": t.safety_prompt,
        "sample": t.sample,
        "experiment_id": d.experiment_id,
    }


def _resolve_after_run(original_path: Path) -> Path:
    """
    Locate an iteration folder after a run that may have renamed it.

    ``fail_iteration_phase`` renames a failed iteration to add a ``-failed``
    suffix, so the path we discovered before the run may no longer exist.
    """
    if original_path.is_dir():
        return original_path
    parent = original_path.parent
    idx = parse_iteration_index(original_path.name)
    if idx is None or not parent.is_dir():
        return original_path
    candidates = [
        c for c in parent.iterdir() if c.is_dir() and parse_iteration_index(c.name) == idx
    ]
    if not candidates:
        return original_path
    non_failed = [c for c in candidates if not iteration_folder_is_failed(c.name)]
    pool = non_failed or candidates
    return max(pool, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_discovery_report(report: DiscoveryReport) -> None:
    logger.info("Discovered %d re-benchable iteration(s).", len(report.discovered))
    for d in report.discovered:
        logger.info(
            "  + %s  [model=%s scenario=%s env=%s sample=%d experiment=%s iteration=%s%s]",
            d.path,
            d.task.model,
            d.task.scenario.id,
            d.task.env.id,
            d.task.sample,
            d.experiment_id,
            d.iteration_id,
            " (failed folder)" if d.is_failed_folder else "",
        )
    logger.info("Skipped %d directory(ies).", len(report.skipped))
    for s in report.skipped:
        logger.info("  - %s  [%s]", s.path, s.reason)


def _print_dry_run_plan(
    groups: dict[tuple[Path, str], list[DiscoveredIteration]],
    manifest_entries: dict[str, ManifestEntry],
    *,
    load_profile: str,
    force: bool,
    results_root: Path,
) -> None:
    logger.info("--dry-run: no cluster contact, no filesystem changes will be made.")
    total_run = total_skip = 0
    for (_sample_dir, _experiment_id), iterations in groups.items():
        for d in iterations:
            key = manifest_key(
                sample_dir=d.sample_dir,
                results_root=results_root,
                experiment_id=d.experiment_id,
                iteration_id=d.iteration_id,
            )
            prior = manifest_entries.get(key)
            if not force and already_reverified(prior, load_profile=load_profile):
                total_skip += 1
                logger.info(
                    "  SKIP  %s (already reverified with load_profile=%r)", d.path, load_profile
                )
                continue
            total_run += 1
            targets = [t for t in stale_artifact_targets(d.path) if t.exists()]
            logger.info(
                "  RUN   %s  would delete: %s",
                d.path,
                ", ".join(str(t) for t in targets) or "(nothing to delete)",
            )
    logger.info(
        "dry-run summary: %d would run, %d would be skipped (already reverified)",
        total_run,
        total_skip,
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _run_group(
    group_key: tuple[Path, str],
    iterations: list[DiscoveredIteration],
    *,
    results_root: Path,
    manifest_entries: dict[str, ManifestEntry],
    manifest_lock: threading.Lock,
    args: argparse.Namespace,
    build_run_config: Any,
    deploy_only_preflight: Any,
    execute_deploy_only_iteration: Any,
    sample_postlude: Any,
) -> None:
    sample_dir, experiment_id = group_key
    task0 = iterations[0].task
    task = _build_task(iterations[0])
    sample = task0.sample

    def _record(d: DiscoveredIteration, entry: ManifestEntry) -> None:
        key = manifest_key(
            sample_dir=d.sample_dir,
            results_root=results_root,
            experiment_id=d.experiment_id,
            iteration_id=d.iteration_id,
        )
        entry.key = key
        with manifest_lock:
            manifest_entries[key] = entry

    def _already_done(d: DiscoveredIteration) -> ManifestEntry | None:
        key = manifest_key(
            sample_dir=d.sample_dir,
            results_root=results_root,
            experiment_id=d.experiment_id,
            iteration_id=d.iteration_id,
        )
        with manifest_lock:
            return manifest_entries.get(key)

    to_run: list[DiscoveredIteration] = []
    for d in iterations:
        prior = _already_done(d)
        if not args.force and already_reverified(prior, load_profile=args.load_profile):
            logger.info("skip %s (already reverified with load_profile=%r)", d.path, args.load_profile)
            _record(
                d,
                ManifestEntry(
                    key="",
                    status="skipped",
                    reason=f"already reverified with load_profile={args.load_profile!r}; pass --force to redo",
                    original_path=str(d.path),
                    task=_task_dict(d),
                    iteration_id=d.iteration_id,
                    load_profile=args.load_profile,
                    timestamp=utc_now(),
                ),
            )
            continue
        to_run.append(d)

    if not to_run:
        return

    # Cleared unconditionally for anything we've decided to (re-)run: our own
    # manifest-based check above is what plays the "skip if already done" role,
    # so RunConfig.force is always True here — otherwise deploy_only's own
    # bench-dir-presence check would treat a freshly-copied original bench run
    # as "already complete" and silently skip it on a first invocation.
    for d in to_run:
        clear_stale_reverify_artifacts(d.path, root=results_root)

    cfg = build_run_config(
        timeout=args.timeout,
        force=True,
        k8s_cluster=args.cluster,
        k8s_iteration=None,
        k8s_iterations=0,
        k8s_wait_timeout=args.k8s_wait_timeout,
        k8s_refinement="auto",
        load_profile=args.load_profile,
        k8s_experiment_id=experiment_id,
        llm_max_cost_usd=None,
        ft_timeout=None,
        num_ports=10000,
        min_port=12345,
        bench_users=args.bench_users,
        bench_spawn_rate=args.bench_spawn_rate,
        bench_run_time=args.bench_run_time,
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
    )

    anchor = to_run[0].path
    ctx = deploy_only_preflight(task, results_root, sample, anchor, cfg)
    if ctx is None:
        logger.warning(
            "preflight failed for sample=%s experiment=%s (anchor=%s); no passing "
            "functional tests found for the anchor iteration or materialized code",
            sample_dir,
            experiment_id,
            anchor,
        )
        for d in to_run:
            _record(
                d,
                ManifestEntry(
                    key="",
                    status="failed",
                    reason="preflight failed: no passing functional tests for the anchor "
                    "iteration and no resolvable code snapshot",
                    original_path=str(d.path),
                    task=_task_dict(d),
                    iteration_id=d.iteration_id,
                    load_profile=args.load_profile,
                    timestamp=utc_now(),
                ),
            )
        return

    for d in to_run:
        run_dir = None
        try:
            run_dir = execute_deploy_only_iteration(ctx, d.path, cfg)
        except Exception as exc:  # noqa: BLE001 - surfaced into the manifest, run continues
            logger.exception("iteration %s raised while reverifying", d.path)
            _record(
                d,
                ManifestEntry(
                    key="",
                    status="failed",
                    reason=f"exception: {exc}",
                    original_path=str(d.path),
                    task=_task_dict(d),
                    iteration_id=d.iteration_id,
                    load_profile=args.load_profile,
                    timestamp=utc_now(),
                ),
            )
            continue

        resolved = _resolve_after_run(d.path)
        if run_dir is not None:
            logger.info("done  %s -> %s", d.path, run_dir)
            _record(
                d,
                ManifestEntry(
                    key="",
                    status="success",
                    reason=None,
                    original_path=str(d.path),
                    task=_task_dict(d),
                    iteration_id=d.iteration_id,
                    load_profile=args.load_profile,
                    timestamp=utc_now(),
                    artifacts={
                        "iteration_dir": str(resolved),
                        "deploy_dir": str(iteration_deploy_dir(resolved)),
                        "bench_dir": str(run_dir),
                    },
                ),
            )
        else:
            meta = read_iteration_meta(resolved)
            reason = meta.get("failure_reason") or (
                "deploy-only run did not complete; see iteration.log / "
                "k8s_bench_skips.log for this sample"
            )
            logger.warning("failed %s: %s", d.path, reason)
            _record(
                d,
                ManifestEntry(
                    key="",
                    status="failed",
                    reason=str(reason),
                    original_path=str(d.path),
                    task=_task_dict(d),
                    iteration_id=d.iteration_id,
                    load_profile=args.load_profile,
                    timestamp=utc_now(),
                    artifacts={"iteration_dir": str(resolved)},
                ),
            )

    sample_postlude(ctx)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    results_root = args.results_dir.expanduser().resolve()
    if not results_root.is_dir():
        logger.error("--results-dir does not exist or is not a directory: %s", results_root)
        return 2

    manifest_path = (
        (args.manifest_path or results_root / "reverification_manifest.json")
        .expanduser()
        .resolve()
    )

    filters = _build_filters(args)
    report = discover_iterations(
        results_root, all_envs=all_envs, all_scenarios=all_scenarios, filters=filters
    )
    _print_discovery_report(report)

    manifest_entries = load_manifest(manifest_path)
    groups = group_by_sample_experiment(report.discovered)

    if args.dry_run:
        _print_dry_run_plan(
            groups, manifest_entries, load_profile=args.load_profile, force=args.force, results_root=results_root
        )
        return 0

    # Every discovered-but-invalid or filtered-out directory gets a manifest
    # entry too, so the manifest is a complete record of "every iteration is
    # either processed or explicitly reported as skipped" — not just the ones
    # that made it through discovery.
    for s in report.skipped:
        key = path_key(s.path, results_root=results_root)
        manifest_entries.setdefault(
            key,
            ManifestEntry(
                key=key,
                status="skipped",
                reason=s.reason,
                original_path=str(s.path),
                task={},
                iteration_id="",
                load_profile=args.load_profile,
                timestamp=utc_now(),
            ),
        )

    if not groups:
        logger.info("No re-benchable iterations to process.")
    else:
        from k8s_bench.cluster import ensure_k8s_cluster_ready
        from k8s_bench.orchestration.deploy_only import execute_deploy_only_iteration
        from k8s_bench.orchestration.preflight import (
            build_run_config,
            deploy_only_preflight,
            sample_postlude,
        )

        ensure_k8s_cluster_ready(logger=logger, profile_name=args.cluster)

        manifest_lock = threading.Lock()

        def run_group(item: tuple[tuple[Path, str], list[DiscoveredIteration]]) -> None:
            group_key, iterations = item
            _run_group(
                group_key,
                iterations,
                results_root=results_root,
                manifest_entries=manifest_entries,
                manifest_lock=manifest_lock,
                args=args,
                build_run_config=build_run_config,
                deploy_only_preflight=deploy_only_preflight,
                execute_deploy_only_iteration=execute_deploy_only_iteration,
                sample_postlude=sample_postlude,
            )

        items = list(groups.items())
        max_workers = max(1, args.parallel)
        if max_workers == 1:
            for item in items:
                run_group(item)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(run_group, item) for item in items]
                for future in as_completed(futures):
                    future.result()

    write_manifest(
        manifest_path,
        manifest_entries,
        results_root=results_root,
        cluster=args.cluster,
        load_profile=args.load_profile,
    )
    logger.info("Manifest written to %s", manifest_path)

    n_success = sum(1 for e in manifest_entries.values() if e.status == "success")
    n_failed = sum(1 for e in manifest_entries.values() if e.status == "failed")
    n_skipped = sum(1 for e in manifest_entries.values() if e.status == "skipped")
    logger.info("Done: %d succeeded, %d failed, %d skipped.", n_success, n_failed, n_skipped)
    return 1 if n_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

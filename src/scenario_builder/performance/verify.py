import io
import pathlib
import re
import tempfile
import time
from dataclasses import dataclass, field

import pandas as pd
import yaml
from config import logger
from performance.locust_exec import (
    run_locust_against_container,
    run_smoke_against_container,
)

from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from scenarios.base import Scenario
from tasks import ContainerRunner, SlotManager


@dataclass(frozen=True)
class LocustRunResult:
    """Outcome of executing a generated Locust file against one reference app."""

    ok: bool
    kind: str
    summary: str
    diagnostic_excerpt: str = ""
    smoke_csv_summary: str = ""
    csv_summary: str = ""


@dataclass(frozen=True)
class EndpointCoverageResult:
    ok: bool
    summary: str
    missing_endpoints: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestHealthResult:
    ok: bool
    summary: str
    request_count: int = 0
    failure_count: int = 0
    failing_endpoints: tuple[str, ...] = ()


def _read_locust_stats(csv_path: pathlib.Path) -> tuple[str, str]:
    """Return an operation-only CSV summary or a diagnostic error string."""
    if not csv_path.exists():
        return "", "Locust did not generate a stats CSV."
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        return "", f"Locust stats CSV could not be parsed: {exc}"
    if "Type" not in df.columns:
        return (
            "",
            f"Locust stats CSV is missing its Type column; columns: {list(df.columns)}",
        )
    # The totals row has Name == "Aggregated" and an empty Type (which pandas
    # reads as NaN, not "" -- a bare `Type != ""` check does not catch it).
    df = df[df["Name"].astype(str).str.strip() != "Aggregated"]
    df = df[df["Type"].notna() & (df["Type"].astype(str).str.strip() != "")]
    return df.to_csv(index=False), ""


def _run_smoke_against_port(
    locust_file: pathlib.Path, temp_dir: pathlib.Path, target_port: int
) -> tuple[str, LocustRunResult | None]:
    """Runs the deterministic smoke pass against an already-running
    container's port. Returns ``(smoke_csv_summary, None)`` on success, or
    ``("", <failure LocustRunResult>)`` on failure -- shared by
    :func:`run_locust` (which keeps its container open afterward for the
    real load pass) and :func:`run_smoke_only` (smoke-only, own container
    lifecycle), so both report failures identically."""
    smoke_csv_prefix = temp_dir / "smoke"
    smoke_proc = run_smoke_against_container(
        locust_file=locust_file,
        csv_prefix=smoke_csv_prefix,
        target_port=target_port,
        logger=logger,
    )
    if smoke_proc.returncode != 0:
        logger.warning(
            "Deterministic Locust smoke run returned non-zero exit code: %s",
            smoke_proc.returncode,
        )
        return "", LocustRunResult(
            ok=False,
            kind="locust_runtime",
            summary="Deterministic endpoint smoke run exited with a non-zero status.",
            diagnostic_excerpt=f"{smoke_proc.stderr}\n{smoke_proc.stdout}",
        )
    smoke_csv_summary, smoke_error = _read_locust_stats(temp_dir / "smoke_stats.csv")
    if smoke_error:
        return "", LocustRunResult(
            ok=False,
            kind="stats_missing_or_invalid",
            summary="Deterministic endpoint smoke run did not produce usable stats.",
            diagnostic_excerpt=smoke_error,
        )
    return smoke_csv_summary, None


def run_smoke_only(
    env: Env, scenario: Scenario, locust_code: str, implementation_files: dict
) -> LocustRunResult:
    """Runs just the deterministic endpoint-coverage smoke pass (build +
    one-user sweep, no real weighted load) against one containerized
    implementation.

    Used to check a Locust script against reference implementations other
    than the one :func:`run_locust` already fully verified (smoke + real
    load) -- see :func:`run_smoke_sweep`. Cheap relative to a full
    verification: no real load pass, so no point paying for one per
    implementation just to compare endpoint-level pass/fail across them.
    """
    import multiprocessing

    num_ports = 1000
    min_port = 20000

    with multiprocessing.Manager() as manager:
        port_manager = SlotManager(manager, num_ports, min_port)

        with tempfile.TemporaryDirectory(prefix=f"locust_smoke_{scenario.id}_") as tmp:
            temp_dir = pathlib.Path(tmp)
            locust_file = temp_dir / "locustfile.py"
            with open(locust_file, "w") as f:
                f.write(locust_code)

            try:
                image_id = env.build_docker_image(
                    implementation_files, COMMON_DOCKER_RUN_COMMANDS, logger
                )
            except Exception as exc:
                logger.exception(
                    "Could not build reference implementation for smoke sweep"
                )
                return LocustRunResult(
                    ok=False,
                    kind="reference_implementation_build",
                    summary="Reference implementation could not be built for Locust smoke verification.",
                    diagnostic_excerpt=str(exc),
                )

            try:
                with ContainerRunner(
                    env,
                    port_manager,
                    image_id,
                    logger,
                    needs_db=scenario.needs_db,
                ) as cr:
                    time.sleep(2)
                    smoke_csv_summary, failure = _run_smoke_against_port(
                        locust_file, temp_dir, cr.port
                    )
                    if failure is not None:
                        return failure
                    return LocustRunResult(
                        ok=True,
                        kind="",
                        summary="Smoke run completed.",
                        smoke_csv_summary=smoke_csv_summary,
                    )
            except Exception as e:
                logger.exception("Error running Locust smoke container")
                return LocustRunResult(
                    ok=False,
                    kind="reference_application_startup",
                    summary="Reference application could not start or serve Locust smoke verification.",
                    diagnostic_excerpt=str(e),
                )


def run_locust(
    env: Env, scenario: Scenario, locust_code: str, implementation_files: dict
) -> LocustRunResult:
    """Runs the provided Locust script against a containerized implementation."""
    import multiprocessing

    # Use a default port range if env.port_range is missing
    # (Env used to have this, but now it uses a single port for the container internal side)
    num_ports = 1000
    min_port = 20000

    with multiprocessing.Manager() as manager:
        port_manager = SlotManager(manager, num_ports, min_port)

        temp_dir = pathlib.Path(f"/tmp/locust_{scenario.id}")
        temp_dir.mkdir(exist_ok=True, parents=True)
        locust_file = temp_dir / "locustfile.py"
        with open(locust_file, "w") as f:
            f.write(locust_code)

        logger.info(f"Building image for {scenario.id} Locust Test")
        # Full build every call (no shared-base-image + mount-code fast path in
        # baxbench's Env), slower than the old ContainerRunnerWithCode approach
        # but uses only what Env actually provides.
        try:
            image_id = env.build_docker_image(
                implementation_files, COMMON_DOCKER_RUN_COMMANDS, logger
            )
        except Exception as exc:
            logger.exception("Could not build the reference implementation")
            return LocustRunResult(
                ok=False,
                kind="reference_implementation_build",
                summary="Reference implementation could not be built for Locust verification.",
                diagnostic_excerpt=str(exc),
            )

        csv_prefix = temp_dir / "result"

        try:
            with ContainerRunner(
                env,
                port_manager,
                image_id,
                logger,
                needs_db=scenario.needs_db,
            ) as cr:
                # wait a bit for server to fully initialize
                time.sleep(2)

                smoke_csv_summary, failure = _run_smoke_against_port(
                    locust_file, temp_dir, cr.port
                )
                if failure is not None:
                    return failure

                proc = run_locust_against_container(
                    locust_file=locust_file,
                    csv_prefix=csv_prefix,
                    target_port=cr.port,
                    logger=logger,
                    run_time_s=15,
                    users=100,
                    spawn_rate=100,
                )

                if proc.returncode != 0:
                    logger.warning(
                        f"Locust returned non-zero exit code: {proc.returncode}"
                    )
                    logger.warning(proc.stderr)
                    return LocustRunResult(
                        ok=False,
                        kind="locust_runtime",
                        summary="Locust exited with a non-zero status.",
                        diagnostic_excerpt=f"{proc.stderr}\n{proc.stdout}",
                    )

                csv_summary, stats_error = _read_locust_stats(
                    temp_dir / "result_stats.csv"
                )
                if stats_error:
                    return LocustRunResult(
                        ok=False,
                        kind="stats_missing_or_invalid",
                        summary="Locust load verification did not produce usable stats.",
                        diagnostic_excerpt=stats_error,
                    )
                return LocustRunResult(
                    ok=True,
                    kind="",
                    summary="Locust execution completed.",
                    smoke_csv_summary=smoke_csv_summary,
                    csv_summary=csv_summary,
                )

        except Exception as e:
            logger.exception("Error running Locust container")
            return LocustRunResult(
                ok=False,
                kind="reference_application_startup",
                summary="Reference application could not start or serve Locust verification.",
                diagnostic_excerpt=str(e),
            )


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _openapi_endpoints(openapi_yaml: str) -> list[tuple[str, str]]:
    """(METHOD, path) pairs declared in the OpenAPI schema, e.g. ("GET", "/users/{id}")."""
    spec = yaml.safe_load(openapi_yaml) or {}
    endpoints = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS and isinstance(operation, dict):
                endpoints.append((method.upper(), path))
    return endpoints


def _path_template_to_regex(path: str) -> re.Pattern:
    """OpenAPI path template -> regex matching concrete request paths, e.g.
    "/users/{id}" -> a pattern matching "/users/42" (path params are
    wildcards; a generated script will have substituted real IDs).

    Tolerates an optional leading HTTP-method token in the matched string
    (e.g. a Locust request named "GET /users/{id}" instead of "/users/{id}"):
    both are established conventions for the ``name=`` kwarg on
    ``self.client.get/post/...`` calls, and the Type column already carries
    the method independently, so either should count as a match.
    """
    escaped = re.escape(path)
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped)
    method_prefix = "|".join(sorted(_HTTP_METHODS, key=str.upper))
    return re.compile(rf"^(?:(?:{method_prefix})\s+)?{pattern}(?:\?.*)?$", re.IGNORECASE)


def _endpoint_row_mask(df: pd.DataFrame, method: str, path: str) -> pd.Series:
    """Boolean mask over ``df`` selecting rows whose (Type, Name) match one
    OpenAPI (method, path) pair (tolerating an optional method-name prefix
    and path-param wildcards; see :func:`_path_template_to_regex`)."""
    types = df["Type"].astype(str).str.strip().str.upper()
    names = df["Name"].astype(str).str.strip()
    pattern = _path_template_to_regex(path)
    return (types == method) & names.str.match(pattern)


def _load_stats_df(csv_results: str) -> tuple[pd.DataFrame | None, str]:
    """Shared CSV loading/column-normalizing for the endpoint-level checks."""
    try:
        df = pd.read_csv(io.StringIO(csv_results))
    except Exception as exc:
        return None, f"Could not parse Locust stats CSV: {exc}"
    df.columns = [c.strip() for c in df.columns]
    required = {"Type", "Name", "Request Count", "Failure Count"}
    if not required.issubset(df.columns):
        return None, f"Locust CSV is missing expected columns, got: {list(df.columns)}"
    return df, ""


def inspect_endpoint_coverage(
    scenario: dict, csv_results: str
) -> EndpointCoverageResult:
    """Deterministically checks every OpenAPI endpoint got at least one
    request with Request Count > 0, by matching the Locust CSV's (Type, Name)
    rows against each endpoint's (METHOD, path template).

    Replaces the old LLM-oracle check — no reasoning-model call involved.
    """
    endpoints = _openapi_endpoints(scenario["schema"])
    if not endpoints:
        return EndpointCoverageResult(
            ok=False,
            summary="Could not find any endpoints in the OpenAPI schema.",
        )

    df, error = _load_stats_df(csv_results)
    if df is None:
        return EndpointCoverageResult(ok=False, summary=error)

    missing = []
    for method, path in endpoints:
        mask = _endpoint_row_mask(df, method, path)
        counts = pd.to_numeric(df.loc[mask, "Request Count"], errors="coerce").fillna(0)
        if not (counts > 0).any():
            missing.append(f"{method} {path}")

    if missing:
        return EndpointCoverageResult(
            ok=False,
            summary=(
                f"{len(missing)}/{len(endpoints)} endpoints had zero requests: "
                + ", ".join(missing)
            ),
            missing_endpoints=tuple(missing),
        )
    return EndpointCoverageResult(
        ok=True, summary=f"All {len(endpoints)} endpoints covered."
    )


def check_endpoint_coverage(scenario: dict, csv_results: str) -> tuple[bool, str]:
    """Compatibility wrapper around :func:`inspect_endpoint_coverage`."""
    result = inspect_endpoint_coverage(scenario, csv_results)
    return result.ok, result.summary


def inspect_request_health(scenario: dict, csv_results: str) -> RequestHealthResult:
    """Reject a nominally-covered script that produces failed requests, and
    identify which specific OpenAPI endpoint(s) are responsible if any single
    endpoint's requests failed 100% of the time.

    The reference implementation is expected to handle normal performance
    traffic.  This deliberately reports a failure count rather than assuming
    the script is at fault; the caller decides whether a failure here is an
    author-retry (scattered/partial failures, likely a script issue) or an
    implementation issue (one or more specific endpoints entirely broken)
    using ``failing_endpoints``.
    """
    df, error = _load_stats_df(csv_results)
    if df is None:
        return RequestHealthResult(ok=False, summary=error)

    requests = pd.to_numeric(df["Request Count"], errors="coerce").fillna(0)
    failures = pd.to_numeric(df["Failure Count"], errors="coerce").fillna(0)
    request_count = int(requests.sum())
    failure_count = int(failures.sum())

    failing_endpoints: tuple[str, ...] = ()
    if failure_count:
        endpoints = _openapi_endpoints(scenario["schema"])
        failing = []
        for method, path in endpoints:
            mask = _endpoint_row_mask(df, method, path)
            ep_requests = pd.to_numeric(
                df.loc[mask, "Request Count"], errors="coerce"
            ).fillna(0).sum()
            ep_failures = pd.to_numeric(
                df.loc[mask, "Failure Count"], errors="coerce"
            ).fillna(0).sum()
            if ep_requests > 0 and ep_failures >= ep_requests:
                failing.append(f"{method} {path}")
        failing_endpoints = tuple(failing)

    if failure_count:
        return RequestHealthResult(
            ok=False,
            summary=(
                f"Locust recorded {failure_count}/{request_count} failed requests "
                "during reference verification."
            ),
            request_count=request_count,
            failure_count=failure_count,
            failing_endpoints=failing_endpoints,
        )
    return RequestHealthResult(
        ok=True,
        summary=f"Locust recorded {request_count} requests with no request failures.",
        request_count=request_count,
    )


def run_smoke_sweep(
    env: Env,
    scenario: Scenario,
    locust_code: str,
    implementations: dict[str, dict],
    *,
    primary_key: str,
    primary_smoke_csv: str,
) -> dict[str, LocustRunResult]:
    """Runs the deterministic smoke pass against every reference
    implementation, reusing ``primary_key``'s already-computed smoke result
    (from :func:`run_locust`) instead of building it a second time."""
    results: dict[str, LocustRunResult] = {
        primary_key: LocustRunResult(
            ok=True,
            kind="",
            summary="Reused the primary implementation's smoke result.",
            smoke_csv_summary=primary_smoke_csv,
        )
    }
    for impl_key, impl_files in implementations.items():
        if impl_key == primary_key:
            continue
        logger.info("Running deterministic smoke pass against %s", impl_key)
        results[impl_key] = run_smoke_only(env, scenario, locust_code, impl_files)
    return results


@dataclass(frozen=True)
class SmokeSweepResult:
    """Cross-implementation view of one Locust script's smoke pass."""

    implementations_tested: tuple[str, ...] = ()
    implementations_unreachable: tuple[str, ...] = ()
    never_covered: tuple[str, ...] = ()
    failing_everywhere: tuple[str, ...] = ()
    failing_by_implementation: dict[str, tuple[str, ...]] = field(default_factory=dict)
    summary: str = ""


def aggregate_smoke_sweep(
    scenario: dict, results: dict[str, LocustRunResult]
) -> SmokeSweepResult:
    """Cross-references every implementation's smoke pass to distinguish a
    script problem (an endpoint fails, or is never reached, the same way for
    every implementation) from an implementation-specific one (some
    implementations handle an endpoint fine, others don't).
    """
    endpoints = _openapi_endpoints(scenario["schema"])
    all_labels = [f"{method} {path}" for method, path in endpoints]

    tested: list[str] = []
    unreachable: list[str] = []
    attempted_by_impl: dict[str, set[str]] = {}
    failed_by_impl: dict[str, set[str]] = {}

    for impl_key, result in results.items():
        if not result.ok or not result.smoke_csv_summary:
            unreachable.append(impl_key)
            continue
        df, error = _load_stats_df(result.smoke_csv_summary)
        if df is None:
            unreachable.append(impl_key)
            continue
        tested.append(impl_key)
        attempted: set[str] = set()
        failed: set[str] = set()
        for method, path in endpoints:
            label = f"{method} {path}"
            mask = _endpoint_row_mask(df, method, path)
            ep_requests = pd.to_numeric(
                df.loc[mask, "Request Count"], errors="coerce"
            ).fillna(0).sum()
            ep_failures = pd.to_numeric(
                df.loc[mask, "Failure Count"], errors="coerce"
            ).fillna(0).sum()
            if ep_requests > 0:
                attempted.add(label)
                if ep_failures >= ep_requests:
                    failed.add(label)
        attempted_by_impl[impl_key] = attempted
        failed_by_impl[impl_key] = failed

    if not tested:
        return SmokeSweepResult(
            implementations_unreachable=tuple(unreachable),
            never_covered=tuple(all_labels),
            summary="No reference implementation could be smoke-tested.",
        )

    never_covered = tuple(
        label
        for label in all_labels
        if all(label not in attempted_by_impl[k] for k in tested)
    )
    failing_everywhere = tuple(
        label
        for label in all_labels
        if label not in never_covered
        and all(
            label in attempted_by_impl[k] and label in failed_by_impl[k]
            for k in tested
        )
    )
    failing_by_implementation: dict[str, tuple[str, ...]] = {}
    for impl_key in tested:
        only_here = tuple(
            label for label in failed_by_impl[impl_key] if label not in failing_everywhere
        )
        if only_here:
            failing_by_implementation[impl_key] = only_here

    summary_parts = [
        f"Smoke-tested {len(tested)}/{len(results)} reference implementation(s)."
    ]
    if unreachable:
        summary_parts.append(
            f"{len(unreachable)} implementation(s) could not be smoke-tested at all."
        )
    if never_covered:
        summary_parts.append(
            f"{len(never_covered)} endpoint(s) never reached by any implementation."
        )
    if failing_everywhere:
        summary_parts.append(
            f"{len(failing_everywhere)} endpoint(s) fail against every implementation "
            "(points at the script, not a specific implementation)."
        )
    if failing_by_implementation:
        summary_parts.append(
            f"{len(failing_by_implementation)} implementation(s) have endpoint(s) "
            "failing only for them."
        )

    return SmokeSweepResult(
        implementations_tested=tuple(tested),
        implementations_unreachable=tuple(unreachable),
        never_covered=never_covered,
        failing_everywhere=failing_everywhere,
        failing_by_implementation=failing_by_implementation,
        summary=" ".join(summary_parts),
    )

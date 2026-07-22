import io
import pathlib
import re
import time
from dataclasses import dataclass

import pandas as pd
import yaml
from config import logger
from performance.locust_exec import run_locust_against_container

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
    df = df[df["Type"].astype(str).str.upper() != "AGGREGATED"]
    df = df[df["Type"].astype(str).str.strip() != ""]
    return df.to_csv(index=False), ""


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

        smoke_csv_prefix = temp_dir / "smoke"
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

                smoke_proc = run_locust_against_container(
                    locust_file=locust_file,
                    csv_prefix=smoke_csv_prefix,
                    target_port=cr.port,
                    logger=logger,
                    run_time_s=10,
                    users=1,
                    spawn_rate=1,
                    smoke=True,
                )
                if smoke_proc.returncode != 0:
                    logger.warning(
                        "Deterministic Locust smoke run returned non-zero exit code: %s",
                        smoke_proc.returncode,
                    )
                    return LocustRunResult(
                        ok=False,
                        kind="locust_runtime",
                        summary="Deterministic endpoint smoke run exited with a non-zero status.",
                        diagnostic_excerpt=f"{smoke_proc.stderr}\n{smoke_proc.stdout}",
                    )
                smoke_csv_summary, smoke_error = _read_locust_stats(
                    temp_dir / "smoke_stats.csv"
                )
                if smoke_error:
                    return LocustRunResult(
                        ok=False,
                        kind="stats_missing_or_invalid",
                        summary="Deterministic endpoint smoke run did not produce usable stats.",
                        diagnostic_excerpt=smoke_error,
                    )

                proc = run_locust_against_container(
                    locust_file=locust_file,
                    csv_prefix=csv_prefix,
                    target_port=cr.port,
                    logger=logger,
                    run_time_s=15,
                    users=100,
                    spawn_rate=100,
                    smoke=False,
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
    wildcards; a generated script will have substituted real IDs)."""
    escaped = re.escape(path)
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped)
    return re.compile(rf"^{pattern}(?:\?.*)?$")


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

    try:
        df = pd.read_csv(io.StringIO(csv_results))
    except Exception as exc:
        return EndpointCoverageResult(
            ok=False,
            summary=f"Could not parse Locust stats CSV: {exc}",
        )
    df.columns = [c.strip() for c in df.columns]
    if not {"Type", "Name", "Request Count"}.issubset(df.columns):
        return EndpointCoverageResult(
            ok=False,
            summary=f"Locust CSV is missing expected columns, got: {list(df.columns)}",
        )

    types = df["Type"].astype(str).str.strip().str.upper()
    names = df["Name"].astype(str).str.strip()
    counts = pd.to_numeric(df["Request Count"], errors="coerce").fillna(0)

    missing = []
    for method, path in endpoints:
        pattern = _path_template_to_regex(path)
        hit = ((types == method) & names.str.match(pattern) & (counts > 0)).any()
        if not hit:
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


def inspect_request_health(csv_results: str) -> RequestHealthResult:
    """Reject a nominally-covered script that produces failed requests.

    The reference implementation is expected to handle normal performance
    traffic.  This deliberately reports a failure count rather than assuming
    the script is at fault; the caller routes it as an author retry only when
    the run itself otherwise completed.
    """
    try:
        df = pd.read_csv(io.StringIO(csv_results))
    except Exception as exc:
        return RequestHealthResult(
            ok=False, summary=f"Could not parse Locust stats CSV: {exc}"
        )
    df.columns = [column.strip() for column in df.columns]
    required = {"Request Count", "Failure Count"}
    if not required.issubset(df.columns):
        return RequestHealthResult(
            ok=False,
            summary=f"Locust CSV is missing expected columns, got: {list(df.columns)}",
        )
    requests = pd.to_numeric(df["Request Count"], errors="coerce").fillna(0)
    failures = pd.to_numeric(df["Failure Count"], errors="coerce").fillna(0)
    request_count = int(requests.sum())
    failure_count = int(failures.sum())
    if failure_count:
        return RequestHealthResult(
            ok=False,
            summary=(
                f"Locust recorded {failure_count}/{request_count} failed requests "
                "during reference verification."
            ),
            request_count=request_count,
            failure_count=failure_count,
        )
    return RequestHealthResult(
        ok=True,
        summary=f"Locust recorded {request_count} requests with no request failures.",
        request_count=request_count,
    )

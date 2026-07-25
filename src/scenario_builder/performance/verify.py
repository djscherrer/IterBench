import io
import pathlib
import re
import tempfile
import time
from dataclasses import dataclass

import pandas as pd
import yaml
from config import logger
from failure import trim
from performance.locust_exec import (
    run_locust_against_container,
    run_smoke_against_container,
)

from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from scenarios.base import Scenario
from tasks import ContainerRunner, SlotManager


@dataclass(frozen=True)
class LocustRunResult:
    """Outcome of the deterministic smoke pass (stage 1) against one
    containerized reference implementation."""

    ok: bool
    kind: str
    summary: str
    diagnostic_excerpt: str = ""
    smoke_csv_summary: str = ""


@dataclass(frozen=True)
class EndpointCoverageResult:
    ok: bool
    summary: str
    missing_endpoints: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeightedLoadResult:
    """Outcome of the weighted load pass (stage 2) against one containerized
    reference implementation. No smoke pass and no coverage gate here --
    stage 1 (:func:`run_smoke_only` against the primary implementation)
    already gates script structure before any script reaches stage 2.
    """

    ok: bool
    kind: str = ""
    summary: str = ""
    diagnostic_excerpt: str = ""
    stats_csv: str = ""
    failures_csv: str = ""
    exceptions_csv: str = ""
    container_log: str = ""


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


def _read_locust_side_csv(csv_path: pathlib.Path) -> str:
    """Best-effort read of a Locust side CSV (failures/exceptions, written
    alongside the stats CSV by the same ``--csv`` flag). Returns "" if the
    file is absent or has no data rows -- both routine, since Locust writes
    a header-only file when nothing of that kind occurred during the run.
    """
    if not csv_path.exists():
        return ""
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return ""
    if df.empty:
        return ""
    return df.to_csv(index=False)


def _read_container_log(container_runner: ContainerRunner) -> str:
    """Best-effort read of the app container's stdout+stderr, taken while the
    container is still running (before ``ContainerRunner`` tears it down).
    Mirrors ``tasks.ContainerRunner``'s own internal capture, via its public
    ``container`` property -- the only way to get the actual application
    traceback behind a failure, since Locust's own CSVs only carry the HTTP
    symptom (e.g. a 500), not why the app produced it.
    """
    try:
        raw = container_runner.container.logs(stdout=True, stderr=True, follow=False)
        return raw.decode(errors="replace").strip()
    except Exception:
        return ""


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

    Used as the stage-1 coverage gate against the primary reference
    implementation, before any script reaches the stage-2 weighted load pass
    (:func:`run_weighted_load_sweep`) against every implementation.
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


def run_weighted_load(
    env: Env,
    scenario: Scenario,
    locust_code: str,
    implementation_files: dict,
    *,
    run_time_s: int = 15,
    users: int = 100,
    spawn_rate: int = 100,
) -> WeightedLoadResult:
    """Runs the weighted load pass (stage 2) against one containerized
    reference implementation: build, boot a backend (+ database, if the
    scenario needs one) container -- no cluster -- and run Locust headless
    for ``run_time_s``. Collects the stats CSV plus Locust's own
    failures/exceptions CSVs, so a genuine problem carries an actual error
    message rather than just a request/failure count.

    Stage 1 (:func:`run_smoke_only`) already gated endpoint coverage against
    the primary implementation before any script reaches this stage, so
    there is no smoke pass and no coverage check here -- this stage exists
    purely to surface real load behavior, per implementation, for the
    author's review.
    """
    import multiprocessing

    num_ports = 1000
    min_port = 20000

    with multiprocessing.Manager() as manager:
        port_manager = SlotManager(manager, num_ports, min_port)

        with tempfile.TemporaryDirectory(prefix=f"locust_load_{scenario.id}_") as tmp:
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
                    "Could not build reference implementation for weighted load pass"
                )
                return WeightedLoadResult(
                    ok=False,
                    kind="reference_implementation_build",
                    summary="Reference implementation could not be built for the weighted load pass.",
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
                    time.sleep(2)

                    proc = run_locust_against_container(
                        locust_file=locust_file,
                        csv_prefix=csv_prefix,
                        target_port=cr.port,
                        logger=logger,
                        run_time_s=run_time_s,
                        users=users,
                        spawn_rate=spawn_rate,
                    )

                    # Locust's CLI intentionally exits non-zero whenever the run
                    # recorded any failed request (its --exit-code-on-error
                    # convention) -- routine here, since surfacing failures is
                    # this stage's whole job, not a sign the harness itself
                    # broke. The only reliable "did this run actually
                    # complete" signal is whether it produced usable stats, so
                    # check that before treating the exit code as meaningful.
                    csv_summary, stats_error = _read_locust_stats(
                        temp_dir / "result_stats.csv"
                    )
                    if stats_error:
                        logger.warning(
                            "Weighted load pass produced no usable stats "
                            "(exit code %s): %s",
                            proc.returncode,
                            stats_error,
                        )
                        return WeightedLoadResult(
                            ok=False,
                            kind="stats_missing_or_invalid",
                            summary="Weighted load pass did not produce usable stats.",
                            diagnostic_excerpt=(
                                f"{stats_error}\n\n{proc.stderr}\n{proc.stdout}"
                            ),
                        )

                    if proc.returncode != 0:
                        logger.info(
                            "Locust exited with status %s but produced valid "
                            "stats; treating as a completed run (Locust exits "
                            "non-zero whenever it recorded failures).",
                            proc.returncode,
                        )

                    return WeightedLoadResult(
                        ok=True,
                        summary="Weighted load pass completed.",
                        stats_csv=csv_summary,
                        failures_csv=_read_locust_side_csv(
                            temp_dir / "result_failures.csv"
                        ),
                        exceptions_csv=_read_locust_side_csv(
                            temp_dir / "result_exceptions.csv"
                        ),
                        container_log=_read_container_log(cr),
                    )

            except Exception as e:
                logger.exception("Error running Locust weighted-load container")
                return WeightedLoadResult(
                    ok=False,
                    kind="reference_application_startup",
                    summary="Reference application could not start or serve the weighted load pass.",
                    diagnostic_excerpt=str(e),
                )


def run_weighted_load_sweep(
    env: Env, scenario: Scenario, locust_code: str, implementations: dict[str, dict]
) -> dict[str, WeightedLoadResult]:
    """Runs the weighted load pass (stage 2) against every reference
    implementation, one container at a time (no cluster)."""
    results: dict[str, WeightedLoadResult] = {}
    for impl_key, impl_files in implementations.items():
        logger.info("Running weighted load pass against %s", impl_key)
        results[impl_key] = run_weighted_load(env, scenario, locust_code, impl_files)
    return results


def build_load_review_report(results: dict[str, WeightedLoadResult]) -> str:
    """Renders per-implementation stage-2 results into a markdown block for
    the author model's review turn: aggregate request/failure counts,
    distinct failure error messages, and unhandled exceptions raised while
    the script ran -- or, for an implementation that couldn't be evaluated
    at all, why not.
    """
    sections = []
    for impl_key, result in results.items():
        lines = [f"### {impl_key}"]
        if not result.ok:
            lines.append(f"Could not be evaluated ({result.kind}): {result.summary}")
            if result.diagnostic_excerpt:
                lines.extend(
                    ["```", trim(result.diagnostic_excerpt, max_chars=800), "```"]
                )
            sections.append("\n".join(lines))
            continue

        df, _ = _load_stats_df(result.stats_csv)
        if df is not None:
            requests = int(
                pd.to_numeric(df["Request Count"], errors="coerce").fillna(0).sum()
            )
            failures = int(
                pd.to_numeric(df["Failure Count"], errors="coerce").fillna(0).sum()
            )
            lines.append(f"Requests: {requests}, Failures: {failures}")

        if result.failures_csv:
            lines.extend(
                [
                    "Distinct failure errors:",
                    "```",
                    trim(result.failures_csv, max_chars=800),
                    "```",
                ]
            )
        if result.exceptions_csv:
            lines.extend(
                [
                    "Unhandled exceptions raised while the script ran:",
                    "```",
                    trim(result.exceptions_csv, max_chars=800),
                    "```",
                ]
            )
        has_finding = bool(result.failures_csv or result.exceptions_csv)
        if has_finding and result.container_log:
            # Locust's own CSVs only carry the HTTP-level symptom (e.g. a
            # 500); the application's own log is what actually shows
            # whether that came from the reference implementation itself
            # (a stack trace) rather than something the script did wrong.
            lines.extend(
                [
                    "Application container log (stdout+stderr) from this run:",
                    "```",
                    trim(result.container_log, max_chars=1600),
                    "```",
                ]
            )
        if not has_finding:
            lines.append("No failures or exceptions.")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


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



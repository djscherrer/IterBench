import io
import pathlib
import re
import time

import pandas as pd
import yaml

from config import logger
from performance.locust_exec import run_locust_against_container
from env.base import COMMON_DOCKER_RUN_COMMANDS, Env
from scenarios.base import Scenario
from tasks import ContainerRunner, SlotManager


def run_locust(
    env: Env, scenario: Scenario, locust_code: str, implementation_files: dict
) -> tuple[bool, str]:
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
        image_id = env.build_docker_image(
            implementation_files, COMMON_DOCKER_RUN_COMMANDS, logger
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

                proc = run_locust_against_container(
                    locust_file=locust_file,
                    csv_prefix=csv_prefix,
                    target_port=cr.port,
                    logger=logger,
                    run_time_s=15,
                    users=1,
                    spawn_rate=1,
                )

                if proc.returncode != 0:
                    logger.warning(
                        f"Locust returned non-zero exit code: {proc.returncode}"
                    )
                    logger.warning(proc.stderr)
                    return False, f"Locust execution failed:\n{proc.stderr}\n{proc.stdout}"

                csv_path = temp_dir / "result_stats.csv"
                if not csv_path.exists():
                    return False, "Locust did not generate a stats CSV"

                df = pd.read_csv(csv_path)
                # Remove the 'Aggregated' row at the bottom
                df = df[df["Type"] != ""]
                csv_summary = df.to_csv(index=False)
                return True, csv_summary

        except Exception as e:
            logger.exception("Error running Locust container")
            return False, f"Failed container execution: {str(e)}"


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


def check_endpoint_coverage(scenario: dict, csv_results: str) -> tuple[bool, str]:
    """Deterministically checks every OpenAPI endpoint got at least one
    request with Request Count > 0, by matching the Locust CSV's (Type, Name)
    rows against each endpoint's (METHOD, path template).

    Replaces the old LLM-oracle check — no reasoning-model call involved.
    """
    endpoints = _openapi_endpoints(scenario["schema"])
    if not endpoints:
        return False, "Could not find any endpoints in the OpenAPI schema."

    df = pd.read_csv(io.StringIO(csv_results))
    df.columns = [c.strip() for c in df.columns]
    if not {"Type", "Name", "Request Count"}.issubset(df.columns):
        return False, f"Locust CSV is missing expected columns, got: {list(df.columns)}"

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
        return False, (
            f"{len(missing)}/{len(endpoints)} endpoints had zero requests: "
            + ", ".join(missing)
        )
    return True, f"All {len(endpoints)} endpoints covered."

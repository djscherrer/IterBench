"""Deterministic endpoint-coverage runner for generated Locust scripts.

Loads a generated locustfile, finds its concrete ``User`` subclass, and calls
every distinct ``@task``-decorated method on it exactly once (in source
definition order) against a live target host, instead of relying on Locust's
own weighted random task selection to eventually reach every task within a
short, fixed-duration run.

Always runs as its own subprocess (like the real ``locust`` CLI invocation
in :mod:`performance.locust_exec`): importing ``locust`` triggers gevent
monkey-patching as a side effect, which must not leak into the parent
orchestrator process.

Writes a Locust-compatible ``<csv-prefix>_stats.csv`` using Locust's own
``StatsCSV`` writer, so it is a drop-in replacement for the stats file a real
``locust --csv`` run would produce: :func:`performance.verify.inspect_endpoint_coverage`
and :func:`performance.verify.inspect_request_health` read it exactly as they
would a real run's.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from pathlib import Path

import locust  # noqa: F401  (import order matters: monkey-patches gevent first)
from locust import User
from locust.env import Environment
from locust.stats import PERCENTILES_TO_REPORT, StatsCSV
from locust.user.task import TaskSet


def _load_locustfile_module(path: Path):
    """Import the generated locustfile as a standalone module."""
    spec = importlib.util.spec_from_file_location("baxbench_smoke_locustfile", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load locustfile spec from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_user_class(module) -> type[User]:
    """The one concrete (non-abstract) User subclass defined in the module."""
    candidates = [
        obj
        for name in dir(module)
        if isinstance(obj := getattr(module, name), type)
        and issubclass(obj, User)
        and obj is not User
        and obj.__module__ == module.__name__
        and not obj.__dict__.get("abstract", False)
    ]
    if not candidates:
        raise RuntimeError(
            "No concrete (non-abstract) locust.User subclass defined directly in "
            "the locustfile."
        )
    if len(candidates) > 1:
        raise RuntimeError(
            "Expected exactly one concrete locust.User subclass in the locustfile, "
            f"found {[c.__name__ for c in candidates]}."
        )
    return candidates[0]


def _unique_tasks(tasks: list) -> list:
    """Dedup a Locust weight-expanded task list, preserving definition order.

    ``@task(n)`` expands to the same callable appearing ``n`` times in
    ``cls.tasks`` (that is how Locust itself represents weight); a plain
    ``dict.fromkeys`` dedup keeps the first-seen (i.e. definition) order.
    """
    return list(dict.fromkeys(tasks))


def _run_task(task, invoke_on) -> tuple[bool, str]:
    """Call one task the way Locust's own ``TaskSet.execute_task`` would.

    A nested ``TaskSet`` class is instantiated and its own unique tasks are
    run once each in turn; a plain task callable is called directly. Any
    exception is caught here so one broken task can't stop the rest of the
    sweep from running.
    """
    try:
        if isinstance(task, type) and issubclass(task, TaskSet):
            nested = task(invoke_on)
            for nested_task in _unique_tasks(nested.tasks):
                ok, message = _run_task(nested_task, nested)
                if not ok:
                    return ok, message
        else:
            task(invoke_on)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        return False, f"{type(exc).__name__}: {exc}"


def run_smoke(locustfile: Path, host: str, csv_prefix: Path) -> int:
    module = _load_locustfile_module(locustfile)
    user_class = _find_user_class(module)
    user_class.host = host

    environment = Environment(user_classes=[user_class], host=host)

    # Environment doesn't wire stats to the request event on its own (only a
    # Runner does, in runners.py); replicate that one listener here.
    def _on_request(
        request_type, name, response_time, response_length, exception=None, **_kwargs
    ):
        environment.stats.log_request(request_type, name, response_time, response_length)
        if exception:
            environment.stats.log_error(request_type, name, exception)

    environment.events.request.add_listener(_on_request)
    environment.events.init.fire(environment=environment, runner=None, web_ui=None)
    environment.events.test_start.fire(environment=environment)

    user = user_class(environment)

    try:
        user.on_start()
    except Exception as exc:
        print(f"on_start() failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    any_failed = False
    for task in _unique_tasks(user_class.tasks):
        ok, message = _run_task(task, user)
        if not ok:
            any_failed = True
            print(
                f"task {getattr(task, '__name__', task)!s} raised: {message}",
                file=sys.stderr,
            )

    csv_prefix.parent.mkdir(parents=True, exist_ok=True)
    stats_csv = StatsCSV(environment, PERCENTILES_TO_REPORT)
    with open(f"{csv_prefix}_stats.csv", "w", newline="", encoding="utf-8") as fh:
        stats_csv.requests_csv(csv.writer(fh))

    # A per-task exception is reported (stderr) and reflected in the stats
    # CSV's failure counts for the caller to interpret; it is not itself a
    # reason to fail the process, since the whole point is to sweep every
    # task once and report what happened, not stop at the first problem.
    if any_failed:
        print("One or more tasks raised during the deterministic sweep; see above.", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locustfile", type=Path)
    parser.add_argument("--host", required=True, help='e.g. "http://localhost:20001"')
    parser.add_argument(
        "--csv-prefix",
        required=True,
        type=Path,
        help="Writes <csv-prefix>_stats.csv, matching `locust --csv <prefix>`.",
    )
    args = parser.parse_args()
    return run_smoke(args.locustfile, args.host, args.csv_prefix)


if __name__ == "__main__":
    raise SystemExit(main())

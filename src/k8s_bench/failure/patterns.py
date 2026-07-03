"""Regex patterns for functional-test and infrastructure log parsing."""

from __future__ import annotations

import re

# `INFO 2026-05-29 04:16:16,211 Functional test func_test_… failed`
FT_STATUS_RE = re.compile(
    r"^INFO\s+\S+\s+\S+\s+Functional test\s+(?P<name>func_test_\w+)\s+(?P<status>passed|failed)\s*$"
)
HARNESS_LINE_RE = re.compile(r"^(INFO|WARNING|ERROR|DEBUG)\s+\d{4}-")
CONTAINER_ERROR_HINT_RE = re.compile(
    r"(error|exception|traceback|fatal|panic|reject)",
    re.IGNORECASE,
)
# Rust / Go / TS compiler diagnostics from docker build output
COMPILE_DIAGNOSTIC_RE = re.compile(
    r"(^error\[E\d+\]|^error:|could not compile|rustc --|"
    r"npm ERR!|error TS\d+|undefined:|cannot find|mismatched types)",
    re.IGNORECASE | re.MULTILINE,
)
DOCKER_BUILD_FAILED_RE = re.compile(
    r"Failed to build docker image|docker\.errors\.BuildError|"
    r"returned a non-zero code: 101",
    re.IGNORECASE,
)
PM2_NOISE_RE = re.compile(
    r"^\s*("
    r"2\d{3}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}: PM2 log:"
    r"|App \[app:\d+\]"
    r"|Database initialized\s*$"
    r"|Server running on port\s*\d*\s*$"
    r")"
)

INFRA_FAILURE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "port_conflict",
        re.compile(r"Bind for [\d.]+:(?P<port>\d+) failed: port is already allocated"),
        "Docker could not bind a host port (port already allocated on the test host)",
    ),
    (
        "container_networking",
        re.compile(r"failed to set up container networking"),
        "Docker failed to program container networking",
    ),
    (
        "image_pull",
        re.compile(r"(error pulling image|manifest unknown|pull access denied)"),
        "Docker could not pull the test image",
    ),
    (
        "postgres_start_timeout",
        re.compile(
            r"(Postgres container .* did not become ready|"
            r"PostgreSQL is ready to accept connections.{0,5}$.{0,5}TimeoutError)"
        ),
        "Postgres test container did not become ready in time",
    ),
    (
        "postgres_start_failure",
        re.compile(r"Failed to start (Postgres|PostgreSQL)"),
        "Postgres test container failed to start",
    ),
    (
        "docker_start_failure",
        re.compile(r"could not start container|Could not start docker container"),
        "Test harness could not start an application container",
    ),
    (
        "server_did_not_start",
        re.compile(r"Server did not start in time"),
        "Application HTTP server did not become reachable in time",
    ),
)

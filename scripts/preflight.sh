#!/bin/bash

# BaxBench - Remote Preflight Script
# Runs `--mode preflight` to validate distributed benchmarking prerequisites
# (disk/remote dir, Docker on container hosts, Python tooling on load hosts, and
# basic load-worker -> load-master connectivity) before running benchmarks.

set -euo pipefail

# --- 1. Remote Benchmarking / Topology ---
BAXBENCH_SYSTEM_TOPOLOGY="${BAXBENCH_SYSTEM_TOPOLOGY:-2C-1B-1DB}"

# Use a stable per-user directory that is always writable.
BENCH_REMOTE_DIR="${BENCH_REMOTE_DIR:-/tmp/baxbench-$USER}"
BENCH_REMOTE_PORT="${BENCH_REMOTE_PORT:-5001}"

# Optional explicit host overrides (only needed if topology has no host mapping).
BENCH_APP_HOST="${BENCH_APP_HOST:-}"
BENCH_APP_HOSTS="${BENCH_APP_HOSTS:-}"
BENCH_LOAD_MASTER="${BENCH_LOAD_MASTER:-}"
BENCH_LOAD_WORKERS="${BENCH_LOAD_WORKERS:-}"
BENCH_DB_HOST="${BENCH_DB_HOST:-}"
BENCH_LB_HOST="${BENCH_LB_HOST:-}"
BENCH_APP_PRIVATE_ADDR="${BENCH_APP_PRIVATE_ADDR:-}"

# --- 2. Speed / Logging Controls ---
BAXBENCH_SSH_MULTIPLEX="${BAXBENCH_SSH_MULTIPLEX:-true}"
BAXBENCH_LOG_COMMANDS="${BAXBENCH_LOG_COMMANDS:-false}"

# Whether to try installing missing python tooling automatically on load hosts.
BAXBENCH_AUTO_INSTALL_REMOTE_DEPS="${BAXBENCH_AUTO_INSTALL_REMOTE_DEPS:-true}"

# --- Execution ---
ARGS=("--mode" "preflight")

add_arg() {
    local flag=$1
    local value=$2
    if [ -n "$value" ]; then
        ARGS+=("$flag")
        for part in $value; do
            ARGS+=("$part")
        done
    fi
}

BASE_ENV=()
BASE_ENV+=("BAXBENCH_SYSTEM_TOPOLOGY=$BAXBENCH_SYSTEM_TOPOLOGY")
if [ "$BAXBENCH_SSH_MULTIPLEX" == "true" ]; then
    BASE_ENV+=("BAXBENCH_SSH_MULTIPLEX=1")
fi
if [ "$BAXBENCH_LOG_COMMANDS" == "true" ]; then
    BASE_ENV+=("BAXBENCH_LOG_COMMANDS=1")
else
    BASE_ENV+=("BAXBENCH_LOG_COMMANDS=0")
fi
if [ "$BAXBENCH_AUTO_INSTALL_REMOTE_DEPS" == "true" ]; then
    BASE_ENV+=("BAXBENCH_AUTO_INSTALL_REMOTE_DEPS=1")
else
    BASE_ENV+=("BAXBENCH_AUTO_INSTALL_REMOTE_DEPS=0")
fi

# Avoid matplotlib cache writes to quota-limited home dirs.
MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/matplotlib-${USER}}"
export MPLCONFIGDIR

add_arg "--bench-remote-dir" "$BENCH_REMOTE_DIR"
add_arg "--bench-remote-port" "$BENCH_REMOTE_PORT"
add_arg "--bench-app-private-addr" "$BENCH_APP_PRIVATE_ADDR"

add_arg "--bench-app-host" "$BENCH_APP_HOST"
add_arg "--bench-app-hosts" "$BENCH_APP_HOSTS"
add_arg "--bench-load-master" "$BENCH_LOAD_MASTER"
add_arg "--bench-load-workers" "$BENCH_LOAD_WORKERS"
add_arg "--bench-db-host" "$BENCH_DB_HOST"
add_arg "--bench-lb-host" "$BENCH_LB_HOST"

echo ""
echo "=== BaxBench preflight ==="
echo "Topology: $BAXBENCH_SYSTEM_TOPOLOGY"
echo "Remote dir: $BENCH_REMOTE_DIR"
echo "Extra env: ${BASE_ENV[*]}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

env "${BASE_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}"

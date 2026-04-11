#!/bin/bash

# BaxBench - Benchmarking Mode Script
# Use this script to run load tests on your generated code (Locust).

# --- 1. Execution Targets ---
MODELS="anthropic/claude-opus-4.6"
ONLY_SAMPLES="0"        # Specify indices, e.g. "0 1 2"; leave empty to use N_SAMPLES
N_SAMPLES=""           # Used if ONLY_SAMPLES is empty

# --- 2. Project Scope ---
ENVS="Python-Flask"                 # e.g. "python-flask javascript-express"
EXCLUDE_ENVS=""
SCENARIOS="Petstore"            # e.g. "Calculator Petstore"
EXCLUDE_SCENARIOS=""
TEMPERATURE="0.4"
SAFETY_PROMPT="high_performance"    # none, generic, specific, high-performance

# --- 3. Locust / Load Configuration ---
# Leave empty to use defaults
BENCH_USERS="20000"          # Number of concurrent users
BENCH_SPAWN_RATE="1000"     # Users spawned per second
BENCH_RUN_TIME="60"      # Duration in seconds (integer)

# --- 4. Remote Benchmarking (Optional) ---
# Single-backend legacy mode (optional)
BENCH_APP_HOST=""       # e.g. user@host
BENCH_APP_PRIVATE_ADDR=""

# Multi-backend mode (Option A: nginx load balancer)
# - One backend per host (max one backend per server)
# - DB runs on BENCH_DB_HOST (defaults to first backend host if empty)
# - LB runs on BENCH_LB_HOST (defaults to BENCH_LOADER_HOST if empty)
BENCH_APP_HOSTS="r630-02 r630-03 r630-04"

# Load generator host (Locust client)
BENCH_LOADER_HOST="r630-08"
# Optional overrides
BENCH_LB_HOST="$BENCH_LOADER_HOST"
BENCH_DB_HOST="r630-05"

# Use a stable per-user directory that is always writable, even if /tmp/baxbench
# was created by root/another user on some machines.
# NOTE: This expands on the remote host when commands run.
BENCH_REMOTE_DIR='$HOME/.cache/baxbench'
BENCH_REMOTE_PORT="5001"

# --- 5. Bench Configuration ---
TIMEOUT=""
MAX_CONCURRENT_RUNS=""
NUM_PORTS=""
MIN_PORT=""
FORCE="true"                # Set to "true" to force test even if results exist

# --- 6. Global Settings ---
RESULTS_DIR=""          # Override default results directory
PORT=""             # Application port

# --- 7. Debug / Teardown Controls ---
# Keep all remote artifacts (containers + ssh tunnels) after a run for debugging.
# Values: "true" to enable, anything else disables.
BAXBENCH_SKIP_TEARDOWN="false"

# Fine-grained keep/reuse toggles for faster iteration across perf runs.
# Values: "true" to enable, anything else disables.
BAXBENCH_KEEP_BACKENDS="true"
BAXBENCH_KEEP_DB="true"
BAXBENCH_KEEP_LB="true"
BAXBENCH_KEEP_TUNNELS="true"
# When reusing an existing DB container, wipe the DB before the run.
BAXBENCH_WIPE_DB_ON_REUSE="true"

# --- 8. Speed / Logging Controls ---
# Reuse SSH connections (ControlMaster) to reduce setup overhead.
BAXBENCH_SSH_MULTIPLEX="true"
# Reduce bench.log noise by not logging every SSH/SCP command at INFO.
BAXBENCH_LOG_COMMANDS="false"
# Collect docker logs from LB/backends/DB into results folder.
BAXBENCH_COLLECT_DOCKER_LOGS="true"

# --- Execution ---
ARGS=("--mode" "bench")

add_arg() {
    local flag=$1
    local value=$2
    if [ -n "$value" ]; then
        ARGS+=("$flag")
        # Split on spaces to handle list arguments (models, scenarios, etc.)
        for part in $value; do
            ARGS+=("$part")
        done
    fi
}

add_flag() {
    if [ "$2" == "true" ]; then
        ARGS+=("$1")
    fi
}

# Mapping variables to flags
add_arg "--models" "$MODELS"
add_arg "--only_samples" "$ONLY_SAMPLES"
add_arg "--n_samples" "$N_SAMPLES"

add_arg "--envs" "$ENVS"
add_arg "--exclude_envs" "$EXCLUDE_ENVS"
add_arg "--scenarios" "$SCENARIOS"
add_arg "--exclude_scenarios" "$EXCLUDE_SCENARIOS"
add_arg "--temperature" "$TEMPERATURE"
add_arg "--safety_prompt" "$SAFETY_PROMPT"

add_arg "--bench-users" "$BENCH_USERS"
add_arg "--bench-spawn-rate" "$BENCH_SPAWN_RATE"
add_arg "--bench-run-time" "$BENCH_RUN_TIME"

add_arg "--bench-app-host" "$BENCH_APP_HOST"
add_arg "--bench-app-hosts" "$BENCH_APP_HOSTS"
add_arg "--bench-app-private-addr" "$BENCH_APP_PRIVATE_ADDR"
add_arg "--bench-loader-host" "$BENCH_LOADER_HOST"
add_arg "--bench-lb-host" "$BENCH_LB_HOST"
add_arg "--bench-db-host" "$BENCH_DB_HOST"
add_arg "--bench-remote-dir" "$BENCH_REMOTE_DIR"
add_arg "--bench-remote-port" "$BENCH_REMOTE_PORT"

add_arg "--timeout" "$TIMEOUT"
add_arg "--max_concurrent_runs" "$MAX_CONCURRENT_RUNS"
add_arg "--num_ports" "$NUM_PORTS"
add_arg "--min_port" "$MIN_PORT"
add_flag "--force" "$FORCE"

add_arg "--results_dir" "$RESULTS_DIR"
add_arg "--port" "$PORT"

echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
EXTRA_ENV=()
if [ "$BAXBENCH_SKIP_TEARDOWN" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_SKIP_TEARDOWN=1")
fi
if [ "$BAXBENCH_SSH_MULTIPLEX" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_SSH_MULTIPLEX=1")
fi
if [ "$BAXBENCH_LOG_COMMANDS" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=1")
else
    EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=0")
fi
if [ "$BAXBENCH_COLLECT_DOCKER_LOGS" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_COLLECT_DOCKER_LOGS=1")
else
    EXTRA_ENV+=("BAXBENCH_COLLECT_DOCKER_LOGS=0")
fi

if [ "$BAXBENCH_KEEP_BACKENDS" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_KEEP_BACKENDS=1")
fi
if [ "$BAXBENCH_KEEP_DB" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_KEEP_DB=1")
fi
if [ "$BAXBENCH_KEEP_LB" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_KEEP_LB=1")
fi
if [ "$BAXBENCH_KEEP_TUNNELS" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_KEEP_TUNNELS=1")
fi
if [ "$BAXBENCH_WIPE_DB_ON_REUSE" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_WIPE_DB_ON_REUSE=1")
else
    EXTRA_ENV+=("BAXBENCH_WIPE_DB_ON_REUSE=0")
fi

echo "Extra env: ${EXTRA_ENV[*]}"
env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}"

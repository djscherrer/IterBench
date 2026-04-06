#!/bin/bash

# BaxBench - Benchmarking Mode Script
# Use this script to run load tests on your generated code (Locust).

# --- 1. Execution Targets ---
MODELS="anthropic/claude-opus-4.6"
ONLY_SAMPLES=""         # Specify indices, e.g. "0 1 2"
N_SAMPLES="10"           # Used if ONLY_SAMPLES is empty

# --- 2. Project Scope ---
ENVS="Python-Flask"                 # e.g. "python-flask javascript-express"
EXCLUDE_ENVS=""
SCENARIOS="Petstore"            # e.g. "Calculator Petstore"
EXCLUDE_SCENARIOS=""
TEMPERATURE="0.4"
SAFETY_PROMPT="high_performance"    # none, generic, specific, high-performance

# --- 3. Locust / Load Configuration ---
# Leave empty to use defaults
BENCH_USERS=""          # Number of concurrent users
BENCH_SPAWN_RATE=""     # Users spawned per second
BENCH_RUN_TIME=""       # Duration (e.g. 1h, 30m, 10s)
BENCH_LOCUSTFILE=""     # Path to custom locustfile

# --- 4. Remote Benchmarking (Optional) ---
BENCH_APP_HOST=""       # e.g. user@host
BENCH_APP_PRIVATE_ADDR="" 
BENCH_LOADER_HOST=""    # e.g. user@host
BENCH_REMOTE_DIR=""
BENCH_REMOTE_PORT=""

# --- 5. Bench Configuration ---
TIMEOUT=""
MAX_CONCURRENT_RUNS=""
NUM_PORTS=""
MIN_PORT=""
FORCE=""                # Set to "true" to force test even if results exist

# --- 6. Global Settings ---
RESULTS_DIR=""          # Override default results directory
PORT=""             # Application port

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
add_arg "--bench-locustfile" "$BENCH_LOCUSTFILE"

add_arg "--bench-app-host" "$BENCH_APP_HOST"
add_arg "--bench-app-private-addr" "$BENCH_APP_PRIVATE_ADDR"
add_arg "--bench-loader-host" "$BENCH_LOADER_HOST"
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
pipenv run python src/main.py "${ARGS[@]}"

#!/bin/bash

# BaxBench - Test Mode Script
# Use this script to run functional and security tests on your generated code.

# --- 1. Execution Targets ---
MODELS="gpt-4o"
ONLY_SAMPLES=""         # Specify indices, e.g. "0 1 2"
N_SAMPLES="5"           # Used if ONLY_SAMPLES is empty

# --- 2. Project Scope ---
ENVS=""                 # e.g. "python-flask javascript-express"
EXCLUDE_ENVS=""
SCENARIOS=""            # e.g. "Calculator Petstore"
EXCLUDE_SCENARIOS=""

# --- 3. Test Configuration ---
TIMEOUT="300"           # Timeout per test in seconds
MAX_CONCURRENT_RUNS=""
NUM_PORTS="10000"
MIN_PORT="12345"
FORCE=""                # Set to "true" to force test even if results exist
PRUNE_DOCKER=""         # Set to "true" to prune containers after run
RUN_SECURITY_TESTS=""    # Set to "true" to run security tests

# --- 4. Global Settings ---
RESULTS_DIR=""          # Override default results directory
PORT="5001"             # Application port

# --- Execution ---
ARGS=("--mode" "test")

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

add_arg "--timeout" "$TIMEOUT"
add_arg "--max_concurrent_runs" "$MAX_CONCURRENT_RUNS"
add_arg "--num_ports" "$NUM_PORTS"
add_arg "--min_port" "$MIN_PORT"
add_flag "--force" "$FORCE"
add_flag "--prune_docker" "$PRUNE_DOCKER"
add_flag "--run_security_tests" "$RUN_SECURITY_TESTS"

add_arg "--results_dir" "$RESULTS_DIR"
add_arg "--port" "$PORT"

echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
pipenv run python src/main.py "${ARGS[@]}"

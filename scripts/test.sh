#!/bin/bash

# BaxBench - Test Mode Script
# Use this script to run functional and security tests on your generated code.

# --- 1. Execution Targets ---
MODELS="deepseek/deepseek-v3.2 openai/gpt-5.4-2026-03-05 anthropic/claude-opus-4-6" # e.g. "gpt-5.4 gpt-5.4-2026-03-05 anthropic/claude-opus-4-6"
ONLY_SAMPLES=""         # Specify indices, e.g. "0 1 2"
N_SAMPLES="5"           # Used if ONLY_SAMPLES is empty

# --- 2. Project Scope ---
ENVS="Python-Flask Go-net-http Rust-Actix JavaScript-express"                 # e.g. "python-flask javascript-express"
EXCLUDE_ENVS=""
SCENARIOS="LexiTally_WordCountDatasets TextWeaver_PatternRewriter BranchWeave_InteractiveStoryGraph"            # e.g. "Calculator Petstore"
EXCLUDE_SCENARIOS=""
TEMPERATURE="0.2"
SAFETY_PROMPT="high_performance"    # none, generic, specific, high-performance


# --- 3. Test Configuration ---
TIMEOUT=""           # Timeout per test in seconds
MAX_CONCURRENT_RUNS=""
NUM_PORTS=""
MIN_PORT=""
FORCE=""                # Set to "true" to force test even if results exist
PRUNE_DOCKER=""         # Set to "true" to prune containers after run
RUN_SECURITY_TESTS=""    # Set to "true" to run security tests
# Space-separated list of modes to run. Example: "false true" to test both layouts.
USE_OPENHANDS_MODES="true false"        # "true" enables OpenHands
# Backwards-compat (if you set USE_OPENHANDS instead of USE_OPENHANDS_MODES).
USE_OPENHANDS=""

# --- 4. Global Settings ---
RESULTS_DIR=""          # Override default results directory
PORT=""             # Application port

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

_openhands_modes="${USE_OPENHANDS_MODES:-$USE_OPENHANDS}"
if [ -z "$_openhands_modes" ]; then
  _openhands_modes="false"
fi

RUN_I=0
for _model in $MODELS; do
  for _openhands in $_openhands_modes; do
    RUN_I=$((RUN_I+1))

    ARGS=("--mode" "test")

    # Mapping variables to flags
    # Run each model separately so provider is inferred from the model prefix (e.g. openai/...).
    add_arg "--models" "$_model"
    add_arg "--only_samples" "$ONLY_SAMPLES"
    add_arg "--n_samples" "$N_SAMPLES"
    add_arg "--temperature" "$TEMPERATURE"

    add_arg "--envs" "$ENVS"
    add_arg "--exclude_envs" "$EXCLUDE_ENVS"
    add_arg "--scenarios" "$SCENARIOS"
    add_arg "--exclude_scenarios" "$EXCLUDE_SCENARIOS"
    add_arg "--safety_prompt" "$SAFETY_PROMPT"

    add_arg "--timeout" "$TIMEOUT"
    add_arg "--max_concurrent_runs" "$MAX_CONCURRENT_RUNS"
    add_arg "--num_ports" "$NUM_PORTS"
    add_arg "--min_port" "$MIN_PORT"
    add_flag "--force" "$FORCE"
    add_flag "--prune_docker" "$PRUNE_DOCKER"
    add_flag "--run_security_tests" "$RUN_SECURITY_TESTS"
    add_flag "--use_openhands" "$_openhands"

    add_arg "--results_dir" "$RESULTS_DIR"
    add_arg "--port" "$PORT"

    echo ""
    echo "=== Test run #$RUN_I: model='${_model}' openhands='${_openhands}' ==="
    echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
    pipenv run python src/main.py "${ARGS[@]}"
  done
done

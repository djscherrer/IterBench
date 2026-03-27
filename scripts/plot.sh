#!/bin/bash

# BaxBench - Plot Mode Script
# Use this script to generate plots from benchmarking results.

# --- 1. Execution Targets ---
MODELS="gpt-4o"
ONLY_SAMPLES=""         # Specify indices, e.g. "0 1 2"
N_SAMPLES="5"           # Used if ONLY_SAMPLES is empty

# --- 2. Scope ---
ENVS=""
SCENARIOS=""

# --- 3. Global Settings ---
RESULTS_DIR=""          # Override default results directory

# --- Execution ---
ARGS=("--mode" "plot")

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

# Mapping variables to flags
add_arg "--models" "$MODELS"
add_arg "--only_samples" "$ONLY_SAMPLES"
add_arg "--n_samples" "$N_SAMPLES"
add_arg "--envs" "$ENVS"
add_arg "--scenarios" "$SCENARIOS"
add_arg "--results_dir" "$RESULTS_DIR"

echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
pipenv run python src/main.py "${ARGS[@]}"

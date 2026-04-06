#!/bin/bash

# BaxBench - Evaluation Mode Script
# Use this script to evaluate results and print performance tables.

# --- 1. Evaluation Targets ---
MODELS="anthropic/claude-opus-4.6"
ONLY_SAMPLES=""         # Specify indices, e.g. "0 1 2"
N_SAMPLES="10"           # Used if ONLY_SAMPLES is empty
KS=""                   # List of k for pass@k, e.g. "1 5"
TEMPERATURE="0.4"
SAFETY_PROMPT="high_performance"    # none, generic, specific, performance, high_performance

# --- 2. Scope ---
ENVS="Python-Flask"
SCENARIOS="Petstore"

# --- 3. Global Settings ---
RESULTS_DIR=""          # Override default results directory

# --- Execution ---
ARGS=("--mode" "evaluate")

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
add_arg "--temperature" "$TEMPERATURE"
add_arg "--safety_prompt" "$SAFETY_PROMPT"
add_arg "--ks" "$KS"
add_arg "--envs" "$ENVS"
add_arg "--scenarios" "$SCENARIOS"
add_arg "--results_dir" "$RESULTS_DIR"

echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
pipenv run python src/main.py "${ARGS[@]}"

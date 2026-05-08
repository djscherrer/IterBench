#!/bin/bash

# BaxBench - Plot Mode Script
# Use this script to generate plots from benchmarking results.

# --- 1. Execution Targets ---
MODELS="anthropic/claude-opus-4.6"
ONLY_SAMPLES="9"        # Specify indices, e.g. "0 1 2"; leave empty to use N_SAMPLES
N_SAMPLES="10"           # Used if ONLY_SAMPLES is empty
TEMPERATURE="0.4"
SAFETY_PROMPT="high_performance"    # none, generic, specific, performance, high_performance

# --- 2. Scope ---
ENVS="Python-Flask"
SCENARIOS="Petstore"            

# --- 2b. Per-run plotting target (optional) ---
# If set to a path, plots only that run directory.
# If set to "AUTO", discover and plot all perf-* run dirs under RESULTS_DIR (or ./results).
PLOT_RUN_DIR="AUTO"

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
add_arg "--temperature" "$TEMPERATURE"
add_arg "--safety_prompt" "$SAFETY_PROMPT"
add_arg "--envs" "$ENVS"
add_arg "--scenarios" "$SCENARIOS"
add_arg "--results_dir" "$RESULTS_DIR"

# --- Run ---
ROOT="${RESULTS_DIR:-results}"
CMD_BASE="pipenv run python src/main.py"

if [ -n "$PLOT_RUN_DIR" ] && [ "$PLOT_RUN_DIR" != "AUTO" ]; then
  # Single run-dir mode.
  ARGS=("--mode" "plot")
  add_arg "--models" "$MODELS"
  add_arg "--only_samples" "$ONLY_SAMPLES"
  add_arg "--n_samples" "$N_SAMPLES"
  add_arg "--temperature" "$TEMPERATURE"
  add_arg "--safety_prompt" "$SAFETY_PROMPT"
  add_arg "--envs" "$ENVS"
  add_arg "--scenarios" "$SCENARIOS"
  add_arg "--results_dir" "$RESULTS_DIR"
  add_arg "--plot-run-dir" "$PLOT_RUN_DIR"

  CMD="$CMD_BASE"
  for a in "${ARGS[@]}"; do
    CMD+=" $(printf "%q" "$a")"
  done
  echo "Executing: $CMD"
  MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/matplotlib-${USER}}"
  export MPLCONFIGDIR
  eval "$CMD"
  exit $?
fi

# AUTO mode: plot every perf-* run dir under ROOT that contains a bench.log.
shopt -s nullglob globstar
RUN_DIRS=( "$ROOT"/**/perf-* )
if [ "${#RUN_DIRS[@]}" -eq 0 ]; then
  echo "No perf-* run dirs found under: $ROOT"
  exit 0
fi

total=0
for d in "${RUN_DIRS[@]}"; do
  [ -d "$d" ] || continue
  [ -f "$d/bench.log" ] || continue
  total=$((total + 1))
done
if [ "$total" -eq 0 ]; then
  echo "No perf-* run dirs with bench.log found under: $ROOT"
  exit 0
fi

count=0
for d in "${RUN_DIRS[@]}"; do
  [ -d "$d" ] || continue
  [ -f "$d/bench.log" ] || continue
  count=$((count + 1))
  echo "== Plotting ($count/$total): $d =="
  ARGS=("--mode" "plot")
  add_arg "--models" "$MODELS"
  add_arg "--only_samples" "$ONLY_SAMPLES"
  add_arg "--n_samples" "$N_SAMPLES"
  add_arg "--temperature" "$TEMPERATURE"
  add_arg "--safety_prompt" "$SAFETY_PROMPT"
  add_arg "--envs" "$ENVS"
  add_arg "--scenarios" "$SCENARIOS"
  add_arg "--results_dir" "$RESULTS_DIR"
  add_arg "--plot-run-dir" "$d"

  CMD="$CMD_BASE"
  for a in "${ARGS[@]}"; do
    CMD+=" $(printf "%q" "$a")"
  done
  echo "Executing: $CMD"
  MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/matplotlib-${USER}}"
  export MPLCONFIGDIR
  eval "$CMD"
done

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

# --- 3. Benchmark Profile ---
# Named load profile from src/distributed_bench/load_profiles/registry.py
# Example values currently available: default, quick-check, stress-heavy
# Bash "tuple" (array): each load profile will be run against each system topology.
# NOTE: Bash arrays are space-separated (NO commas). Good:
#   BAXBENCH_LOAD_PROFILE=("default" "quick-check")
BAXBENCH_LOAD_PROFILE=("stairs-100-100-30-20")

# Optional one-off overrides (leave empty to use selected load profile).
BENCH_USERS=""
BENCH_SPAWN_RATE=""
BENCH_RUN_TIME=""

# --- 4. Remote Benchmarking / Topology ---
# Named system topology profile from src/distributed_bench/system_configs/registry.py
# Bash "tuple" (array): each system topology will be run for each load profile.
# NOTE: Bash arrays are space-separated (NO commas). Good:
#   BAXBENCH_SYSTEM_TOPOLOGY=("2C-1LB-2B-1DB" "2C-1B-1DB")
BAXBENCH_SYSTEM_TOPOLOGY=("2C-1LB-2B-1DB" "2C-1B-1DB")


# Use a stable per-user directory that is always writable, even if /tmp/baxbench
# was created by root/another user on some machines.
# NOTE: This expands on the remote host when commands run.
BENCH_REMOTE_DIR='$HOME/.cache/baxbench'
BENCH_REMOTE_PORT="5001"

# --- 5. Bench Configuration ---
TIMEOUT="20"
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
BAXBENCH_KEEP_BACKENDS="false"
BAXBENCH_KEEP_DB="false"
BAXBENCH_KEEP_LB="false"
BAXBENCH_KEEP_TUNNELS="false"
# Named topology profile from src/distributed_bench/system_configs/registry.py

# When reusing an existing DB container (see BAXBENCH_KEEP_DB), reset application
# data before the run: TRUNCATE public app tables (keeps schema + migration tables),
# reset pg_stat_statements. Does not DROP SCHEMA.
BAXBENCH_WIPE_DB_ON_REUSE="true"

# --- 8. Speed / Logging Controls ---
# Reuse SSH connections (ControlMaster) to reduce setup overhead.
BAXBENCH_SSH_MULTIPLEX="true"
# Reduce bench.log noise by not logging every SSH/SCP command at INFO.
BAXBENCH_LOG_COMMANDS="false"
# Collect docker logs from LB/backends/DB into results folder.
BAXBENCH_COLLECT_DOCKER_LOGS="true"

# --- Post-bench plotting (same workflow as scripts/plot.sh --plot-run-dir) ---
# When "true", after bench finishes, generates per-run plots (backend vs DB, throughput,
# remote perf) for each perf-* directory created in this invocation.
PLOT_AFTER_BENCH="true"

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
add_arg "--bench-remote-dir" "$BENCH_REMOTE_DIR"
add_arg "--bench-remote-port" "$BENCH_REMOTE_PORT"

add_arg "--timeout" "$TIMEOUT"
add_arg "--max_concurrent_runs" "$MAX_CONCURRENT_RUNS"
add_arg "--num_ports" "$NUM_PORTS"
add_arg "--min_port" "$MIN_PORT"
add_flag "--force" "$FORCE"

add_arg "--results_dir" "$RESULTS_DIR"
add_arg "--port" "$PORT"

if [ "$PLOT_AFTER_BENCH" == "true" ]; then
    ARGS+=("--plot-after-bench")
fi

echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
BASE_ENV=()
if [ "$BAXBENCH_SKIP_TEARDOWN" == "true" ]; then
    BASE_ENV+=("BAXBENCH_SKIP_TEARDOWN=1")
fi
if [ "$BAXBENCH_SSH_MULTIPLEX" == "true" ]; then
    BASE_ENV+=("BAXBENCH_SSH_MULTIPLEX=1")
fi
if [ "$BAXBENCH_LOG_COMMANDS" == "true" ]; then
    BASE_ENV+=("BAXBENCH_LOG_COMMANDS=1")
else
    BASE_ENV+=("BAXBENCH_LOG_COMMANDS=0")
fi
if [ "$BAXBENCH_COLLECT_DOCKER_LOGS" == "true" ]; then
    BASE_ENV+=("BAXBENCH_COLLECT_DOCKER_LOGS=1")
else
    BASE_ENV+=("BAXBENCH_COLLECT_DOCKER_LOGS=0")
fi

if [ "$BAXBENCH_KEEP_BACKENDS" == "true" ]; then
    BASE_ENV+=("BAXBENCH_KEEP_BACKENDS=1")
fi
if [ "$BAXBENCH_KEEP_DB" == "true" ]; then
    BASE_ENV+=("BAXBENCH_KEEP_DB=1")
fi
if [ "$BAXBENCH_KEEP_LB" == "true" ]; then
    BASE_ENV+=("BAXBENCH_KEEP_LB=1")
fi
if [ "$BAXBENCH_KEEP_TUNNELS" == "true" ]; then
    BASE_ENV+=("BAXBENCH_KEEP_TUNNELS=1")
fi
if [ "$BAXBENCH_WIPE_DB_ON_REUSE" == "true" ]; then
    BASE_ENV+=("BAXBENCH_WIPE_DB_ON_REUSE=1")
else
    BASE_ENV+=("BAXBENCH_WIPE_DB_ON_REUSE=0")
fi

MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/matplotlib-${USER}}"
export MPLCONFIGDIR

RUN_I=0
for topo in "${BAXBENCH_SYSTEM_TOPOLOGY[@]}"; do
  for profile in "${BAXBENCH_LOAD_PROFILE[@]}"; do
    RUN_I=$((RUN_I+1))
    EXTRA_ENV=("${BASE_ENV[@]}")
    if [ -n "$topo" ]; then
        EXTRA_ENV+=("BAXBENCH_SYSTEM_TOPOLOGY=$topo")
    fi
    if [ -n "$profile" ]; then
        EXTRA_ENV+=("BAXBENCH_LOAD_PROFILE=$profile")
    fi

    echo ""
    echo "=== Run #$RUN_I: topology='$topo' load_profile='$profile' ==="
    echo "Extra env: ${EXTRA_ENV[*]}"
    env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}"
    RC=$?
    if [ $RC -ne 0 ]; then
        echo "Run #$RUN_I failed (exit=$RC). Stopping."
        exit $RC
    fi
  done
done

#!/bin/bash

# BaxBench - Flexible pipeline runner
# Runs a configurable sequence of modes (generate/test/bench/evaluate/plot)
# for each model and safety prompt.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

# --- 1. Mode Selection ---
# Available: generate test bench evaluate plot
MODES="test bench evaluate plot"
# Plot can run once globally (recommended) or for each model/safety combination.
PLOT_SCOPE="global" # global | per_combo

# --- 2. Execution Targets ---
MODELS="anthropic/claude-opus-4.6"
SAFETY_PROMPTS="performance" # none, generic, specific, performance, high_performance
PROVIDER="openrouter"

# --- 3. Scope ---
ENVS="Python-Flask"
SCENARIOS="Petstore"
EXCLUDE_ENVS=""
EXCLUDE_SCENARIOS=""

N_SAMPLES="10"
ONLY_SAMPLES=""
TEMPERATURE="0.4"
SPEC_TYPE="openapi"

# --- 4. Shared Runtime Settings ---
RESULTS_DIR=""
PORT=""
MAX_CONCURRENT_RUNS=""
TIMEOUT=""
NUM_PORTS=""
MIN_PORT=""
FORCE=""

# --- 5. Generation/Test Options ---
REASONING_EFFORT="high"
MAX_RETRIES=""
BASE_DELAY=""
MAX_DELAY=""
SKIP_FAILED=""
VLLM_PORT=""
USE_STUBS="true"
PRUNE_DOCKER=""
RUN_SECURITY_TESTS=""

# --- 6. Agent Options (Generate) ---
USE_OPENHANDS=""
USE_CLAUDE_AGENT=""
AGENT_CLS="CodeActAgent"
AGENT_MAX_ITERATIONS="50"
AGENT_MAX_COST=""
AGENT_MAX_TOKENS=""

# --- 7. Remote Bench Options ---
BENCH_APP_HOST=""
BENCH_APP_PRIVATE_ADDR=""
BENCH_LOADER_HOST=""
BENCH_REMOTE_DIR=""
BENCH_REMOTE_PORT=""
BENCH_USERS=""
BENCH_SPAWN_RATE=""
BENCH_RUN_TIME=""

# --- 8. Control Behavior ---
STOP_ON_ERROR="false"   # true: stop at first failure, false: continue and summarize failures

FAILURES=()

append_common_args() {
    local -n args_ref=$1
    if [ -n "$N_SAMPLES" ]; then args_ref+=("--n_samples" "$N_SAMPLES"); fi
    if [ -n "$ONLY_SAMPLES" ]; then
        args_ref+=("--only_samples")
        for part in $ONLY_SAMPLES; do args_ref+=("$part"); done
    fi
    if [ -n "$TEMPERATURE" ]; then args_ref+=("--temperature" "$TEMPERATURE"); fi
    if [ -n "$SPEC_TYPE" ]; then args_ref+=("--spec_type" "$SPEC_TYPE"); fi
    if [ -n "$REASONING_EFFORT" ]; then args_ref+=("--reasoning_effort" "$REASONING_EFFORT"); fi
    if [ -n "$PROVIDER" ]; then args_ref+=("--provider" "$PROVIDER"); fi
    if [ -n "$ENVS" ]; then
        args_ref+=("--envs")
        for part in $ENVS; do args_ref+=("$part"); done
    fi
    if [ -n "$EXCLUDE_ENVS" ]; then
        args_ref+=("--exclude_envs")
        for part in $EXCLUDE_ENVS; do args_ref+=("$part"); done
    fi
    if [ -n "$SCENARIOS" ]; then
        args_ref+=("--scenarios")
        for part in $SCENARIOS; do args_ref+=("$part"); done
    fi
    if [ -n "$EXCLUDE_SCENARIOS" ]; then
        args_ref+=("--exclude_scenarios")
        for part in $EXCLUDE_SCENARIOS; do args_ref+=("$part"); done
    fi
    if [ -n "$MAX_CONCURRENT_RUNS" ]; then args_ref+=("--max_concurrent_runs" "$MAX_CONCURRENT_RUNS"); fi
    if [ -n "$TIMEOUT" ]; then args_ref+=("--timeout" "$TIMEOUT"); fi
    if [ -n "$NUM_PORTS" ]; then args_ref+=("--num_ports" "$NUM_PORTS"); fi
    if [ -n "$MIN_PORT" ]; then args_ref+=("--min_port" "$MIN_PORT"); fi
    if [ -n "$RESULTS_DIR" ]; then args_ref+=("--results_dir" "$RESULTS_DIR"); fi
    if [ -n "$PORT" ]; then args_ref+=("--port" "$PORT"); fi
}

add_flag_arg() {
    local -n args_ref=$1
    local flag=$2
    local value=$3
    if [ "$value" = "true" ]; then
        args_ref+=("$flag")
    fi
}

mode_enabled() {
    local wanted="$1"
    for mode in $MODES; do
        if [ "$mode" = "$wanted" ]; then
            return 0
        fi
    done
    return 1
}

run_step() {
    local label="$1"
    shift
    echo
    echo "===== $label ====="
    echo "Executing: pipenv run python src/main.py $*"
    (cd "$REPO_ROOT" && pipenv run python src/main.py "$@")
    local rc=$?
    if [ $rc -ne 0 ]; then
        FAILURES+=("$label (exit $rc)")
        if [ "$STOP_ON_ERROR" = "true" ]; then
            echo "Stopping due to failure: $label"
            exit $rc
        fi
    fi
}

run_mode_for_combo() {
    local mode="$1"
    local model="$2"
    local safety_prompt="$3"

    COMMON_ARGS=("--models" "$model")
    if [ -n "$safety_prompt" ]; then
        COMMON_ARGS+=("--safety_prompt" "$safety_prompt")
    fi
    append_common_args COMMON_ARGS

    case "$mode" in
        generate)
            MODE_ARGS=("--mode" "generate")
            MODE_ARGS+=("${COMMON_ARGS[@]}")
            if [ -n "$MAX_RETRIES" ]; then MODE_ARGS+=("--max_retries" "$MAX_RETRIES"); fi
            if [ -n "$BASE_DELAY" ]; then MODE_ARGS+=("--base_delay" "$BASE_DELAY"); fi
            if [ -n "$MAX_DELAY" ]; then MODE_ARGS+=("--max_delay" "$MAX_DELAY"); fi
            if [ -n "$VLLM_PORT" ]; then MODE_ARGS+=("--vllm_port" "$VLLM_PORT"); fi
            if [ -n "$AGENT_CLS" ]; then MODE_ARGS+=("--agent_cls" "$AGENT_CLS"); fi
            if [ -n "$AGENT_MAX_ITERATIONS" ]; then MODE_ARGS+=("--agent_max_iterations" "$AGENT_MAX_ITERATIONS"); fi
            if [ -n "$AGENT_MAX_COST" ]; then MODE_ARGS+=("--agent_max_cost" "$AGENT_MAX_COST"); fi
            if [ -n "$AGENT_MAX_TOKENS" ]; then MODE_ARGS+=("--agent_max_tokens" "$AGENT_MAX_TOKENS"); fi
            add_flag_arg MODE_ARGS "--skip_failed" "$SKIP_FAILED"
            add_flag_arg MODE_ARGS "--use_openhands" "$USE_OPENHANDS"
            add_flag_arg MODE_ARGS "--use_claude_agent" "$USE_CLAUDE_AGENT"
            add_flag_arg MODE_ARGS "--use_stubs" "$USE_STUBS"
            add_flag_arg MODE_ARGS "--force" "$FORCE"
            run_step "generate model=$model safety=$safety_prompt" "${MODE_ARGS[@]}"
            ;;
        test)
            MODE_ARGS=("--mode" "test")
            MODE_ARGS+=("${COMMON_ARGS[@]}")
            add_flag_arg MODE_ARGS "--prune_docker" "$PRUNE_DOCKER"
            add_flag_arg MODE_ARGS "--run_security_tests" "$RUN_SECURITY_TESTS"
            add_flag_arg MODE_ARGS "--force" "$FORCE"
            run_step "test model=$model safety=$safety_prompt" "${MODE_ARGS[@]}"
            ;;
        bench)
            MODE_ARGS=("--mode" "bench")
            MODE_ARGS+=("${COMMON_ARGS[@]}")
            if [ -n "$BENCH_APP_HOST" ]; then MODE_ARGS+=("--bench-app-host" "$BENCH_APP_HOST"); fi
            if [ -n "$BENCH_APP_PRIVATE_ADDR" ]; then MODE_ARGS+=("--bench-app-private-addr" "$BENCH_APP_PRIVATE_ADDR"); fi
            if [ -n "$BENCH_LOADER_HOST" ]; then MODE_ARGS+=("--bench-loader-host" "$BENCH_LOADER_HOST"); fi
            if [ -n "$BENCH_REMOTE_DIR" ]; then MODE_ARGS+=("--bench-remote-dir" "$BENCH_REMOTE_DIR"); fi
            if [ -n "$BENCH_REMOTE_PORT" ]; then MODE_ARGS+=("--bench-remote-port" "$BENCH_REMOTE_PORT"); fi
            if [ -n "$BENCH_USERS" ]; then MODE_ARGS+=("--bench-users" "$BENCH_USERS"); fi
            if [ -n "$BENCH_SPAWN_RATE" ]; then MODE_ARGS+=("--bench-spawn-rate" "$BENCH_SPAWN_RATE"); fi
            if [ -n "$BENCH_RUN_TIME" ]; then MODE_ARGS+=("--bench-run-time" "$BENCH_RUN_TIME"); fi
            add_flag_arg MODE_ARGS "--force" "$FORCE"
            run_step "bench model=$model safety=$safety_prompt" "${MODE_ARGS[@]}"
            ;;
        evaluate)
            MODE_ARGS=("--mode" "evaluate")
            MODE_ARGS+=("${COMMON_ARGS[@]}")
            run_step "evaluate model=$model safety=$safety_prompt" "${MODE_ARGS[@]}"
            ;;
        plot)
            if [ "$PLOT_SCOPE" = "per_combo" ]; then
                MODE_ARGS=("--mode" "plot")
                MODE_ARGS+=("${COMMON_ARGS[@]}")
                run_step "plot model=$model safety=$safety_prompt" "${MODE_ARGS[@]}"
            fi
            ;;
        *)
            FAILURES+=("invalid mode '$mode'")
            if [ "$STOP_ON_ERROR" = "true" ]; then
                echo "Stopping due to invalid mode: $mode"
                exit 1
            fi
            ;;
    esac
}

echo "MODES: $MODES"
echo "MODELS: $MODELS"
echo "SAFETY_PROMPTS: $SAFETY_PROMPTS"
echo "ENVS: $ENVS"
echo "SCENARIOS: $SCENARIOS"

for model in $MODELS; do
    for safety_prompt in $SAFETY_PROMPTS; do
        for mode in $MODES; do
            run_mode_for_combo "$mode" "$model" "$safety_prompt"
        done
    done
done

if mode_enabled "plot" && [ "$PLOT_SCOPE" = "global" ]; then
    PLOT_ARGS=("--mode" "plot")
    append_common_args PLOT_ARGS
    PLOT_ARGS+=("--models")
    for model in $MODELS; do PLOT_ARGS+=("$model"); done
    run_step "plot all models" "${PLOT_ARGS[@]}"
fi

echo
if [ ${#FAILURES[@]} -eq 0 ]; then
    echo "Perf suite completed with no command failures."
else
    echo "Perf suite finished with failures:"
    for f in "${FAILURES[@]}"; do
        echo " - $f"
    done
    exit 1
fi

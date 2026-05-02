#!/bin/bash

# BaxBench - Generation Mode Script
# Use this script to generate code for your scenarios.

# --- 1. Model Selection ---
MODELS="openai/gpt-5.4-2026-03-05 anthropic/claude-opus-4-6 deepseek/deepseek-v3.2" # e.g. "gpt-5.4 gpt-5.4-2026-03-05 anthropic/claude-opus-4-6"
TEMPERATURE="0.2"
REASONING_EFFORT=""      # low, medium, high

# --- 2. Project Scope ---
ENVS="Python-Flask Go-net-http Rust-Actix JavaScript-express"                 # e.g. "python-flask javascript-express"
EXCLUDE_ENVS=""
SCENARIOS="LexiTally_WordCountDatasets TextWeaver_PatternRewriter BranchWeave_InteractiveStoryGraph"            # e.g. "Calculator Petstore"
EXCLUDE_SCENARIOS=""
SPEC_TYPE=""     # openapi, text, json_api
SAFETY_PROMPT="high_performance"    # none, generic, specific, performance, high_performance

# --- 3. Generation Configuration ---
N_SAMPLES="5"
MAX_RETRIES=""
BASE_DELAY=""
MAX_DELAY=""
FORCE=""                # Set to "true" to force generation
SKIP_FAILED=""           # Set to "true" to skip failed tasks
VLLM_PORT=""
USE_STUBS=""        # Whether to use code stubs

# --- 4. Agent Configuration (Optional) ---
# Space-separated list of modes to run. Example: "false true" to run both.
USE_OPENHANDS_MODES="true false"        # "true" enables OpenHands
# Backwards-compat (if you set USE_OPENHANDS instead of USE_OPENHANDS_MODES).
USE_OPENHANDS=""
USE_CLAUDE_AGENT=""      # Set to "true" to use Claude Agent SDK
AGENT_CLS=""
AGENT_MAX_ITERATIONS=""
AGENT_MAX_COST=""
AGENT_MAX_TOKENS=""

# --- 5. Global Settings ---
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

    ARGS=("--mode" "generate")

    # Mapping variables to flags
    # Run each model separately so provider is inferred from the model prefix (e.g. openai/...).
    add_arg "--models" "$_model"
    add_arg "--temperature" "$TEMPERATURE"
    add_arg "--reasoning_effort" "$REASONING_EFFORT"

    add_arg "--envs" "$ENVS"
    add_arg "--exclude_envs" "$EXCLUDE_ENVS"
    add_arg "--scenarios" "$SCENARIOS"
    add_arg "--exclude_scenarios" "$EXCLUDE_SCENARIOS"
    add_arg "--spec_type" "$SPEC_TYPE"
    add_arg "--safety_prompt" "$SAFETY_PROMPT"

    add_arg "--n_samples" "$N_SAMPLES"
    add_arg "--max_retries" "$MAX_RETRIES"
    add_arg "--base_delay" "$BASE_DELAY"
    add_arg "--max_delay" "$MAX_DELAY"
    add_flag "--force" "$FORCE"
    add_flag "--skip_failed" "$SKIP_FAILED"
    add_arg "--vllm_port" "$VLLM_PORT"
    add_flag "--use_stubs" "$USE_STUBS"

    add_flag "--use_openhands" "$_openhands"
    add_flag "--use_claude_agent" "$USE_CLAUDE_AGENT"
    add_arg "--agent_cls" "$AGENT_CLS"
    add_arg "--agent_max_iterations" "$AGENT_MAX_ITERATIONS"
    add_arg "--agent_max_cost" "$AGENT_MAX_COST"
    add_arg "--agent_max_tokens" "$AGENT_MAX_TOKENS"

    add_arg "--results_dir" "$RESULTS_DIR"
    add_arg "--port" "$PORT"

    echo ""
    echo "=== Generate run #$RUN_I: model='${_model}' openhands='${_openhands}' ==="
    echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
    pipenv run python src/main.py "${ARGS[@]}"
  done
done
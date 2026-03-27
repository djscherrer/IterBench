#!/bin/bash

# BaxBench - Generation Mode Script
# Use this script to generate code for your scenarios.

# --- 1. Model Selection ---
MODELS="gpt-4o"
TEMPERATURE="0.2"
REASONING_EFFORT="high" # low, medium, high
PROVIDER=""              # openai, anthropic, together_ai, openrouter, swissai, vllm

# --- 2. Project Scope ---
ENVS=""                 # e.g. "python-flask javascript-express"
EXCLUDE_ENVS=""
SCENARIOS=""            # e.g. "Calculator Petstore"
EXCLUDE_SCENARIOS=""
SPEC_TYPE="openapi"     # openapi, text, json_api
SAFETY_PROMPT="none"    # none, generic, specific, performance, high_performance

# --- 3. Generation Configuration ---
N_SAMPLES="5"
MAX_RETRIES="2"
BASE_DELAY="1.0"
MAX_DELAY="128.0"
FORCE=""                # Set to "true" to force generation
SKIP_FAILED=""           # Set to "true" to skip failed tasks
VLLM_PORT="8000"
USE_STUBS="true"        # Whether to use code stubs

# --- 4. Agent Configuration (Optional) ---
USE_OPENHANDS=""        # Set to "true" to use OpenHands
USE_CLAUDE_AGENT=""      # Set to "true" to use Claude Agent SDK
AGENT_CLS="CodeActAgent"
AGENT_MAX_ITERATIONS="50"
AGENT_MAX_COST=""
AGENT_MAX_TOKENS=""

# --- 5. Global Settings ---
RESULTS_DIR=""          # Override default results directory
PORT="5001"             # Application port

# --- Execution ---
ARGS=("--mode" "generate")

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
add_arg "--temperature" "$TEMPERATURE"
add_arg "--reasoning_effort" "$REASONING_EFFORT"
add_arg "--provider" "$PROVIDER"

add_arg "--envs" "$ENVS"
add_arg "--exclude_envs" "$EXCLUDE_ENVS"
add_arg "--scenarios" "$SCENARIOS"
add_arg "--exclude_scenarios" "$EXCLUDE_SCENARIOS"
add_arg "--spec_type" "$SPEC_TYPE"
add_arg "--safety_prompt" "$SAFETY_PROMPT"

add_arg "--n_samples" "$N_SAMPLES"
add_arg "--max_retries" "$MAX_retries"
add_arg "--base_delay" "$BASE_DELAY"
add_arg "--max_delay" "$MAX_DELAY"
add_flag "--force" "$FORCE"
add_flag "--skip_failed" "$SKIP_FAILED"
add_arg "--vllm_port" "$VLLM_PORT"
add_flag "--use_stubs" "$USE_STUBS"

add_flag "--use_openhands" "$USE_OPENHANDS"
add_flag "--use_claude_agent" "$USE_CLAUDE_AGENT"
add_arg "--agent_cls" "$AGENT_CLS"
add_arg "--agent_max_iterations" "$AGENT_MAX_ITERATIONS"
add_arg "--agent_max_cost" "$AGENT_MAX_COST"
add_arg "--agent_max_tokens" "$AGENT_MAX_TOKENS"

add_arg "--results_dir" "$RESULTS_DIR"
add_arg "--port" "$PORT"

echo "Executing: pipenv run python src/main.py ${ARGS[@]}"
pipenv run python src/main.py "${ARGS[@]}"

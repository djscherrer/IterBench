#!/bin/bash
# BaxBench - scenario bootstrapping (AutoBaxBuilder / scenario_builder)
#
# Generates scenario(s) end-to-end via `src/main.py --mode build-scenarios`,
# which dispatches straight to scenario_builder/orchestrator.py (its own
# separate CLI — see src/scenario_builder/config.py for the full flag list).
#
# Set MODES to any subset of scenario_builder's mutually-exclusive generation
# steps (space-separated, any order you like below — always actually RUN in
# the fixed pipeline order they depend on each other in):
#   generate_scenarios   generate a new scenario idea + OpenAPI spec + text spec
#                         (takes no --scenario; runs once, not once per SCENARIOS entry)
#   generate_tests       generate + iterate functional tests (needs SCENARIO)
#   generate_exploits    generate + iterate security exploits (needs SCENARIO)
#   generate_performance generate + verify a Locust script (needs SCENARIO)
#   export_latest        promote the latest snapshot into scenarios/generated_scenarios/
#
# Every mode except generate_scenarios runs once per entry in SCENARIOS, in a
# double loop: for each pipeline-ordered mode, for each scenario.

set -euo pipefail

# === EDIT THESE ===========================
MODES="generate_tests generate_performance export_latest"
SCENARIOS="LockerDropParcelExchange"

# Model(s) whose reference solutions get generated & tested (space-separated).
# Prefix with a native provider (openai/, anthropic/, together_ai/, swissai/,
# openrouter/) to route there directly, e.g. "openai/gpt-5-2025-08-07"
# "anthropic/claude-sonnet-4-20250514" — same convention as MODELS in
# bench_k8s.sh. Anything without a recognized prefix (e.g. "z-ai/glm-5.2",
# "deepseek/deepseek-v3.2") is passed through as-is to OpenRouter.
MODELS="z-ai/glm-5.2 google/gemini-3.6-flash deepseek/deepseek-v4-flash"
# Env(s) to generate/test solutions in, e.g. "Python-Flask" (space-separated).
ENVS="Python-Flask"

# Model powering scenario_builder's own agent/reasoning steps (idea, spec,
# exploit, and functional-test generation + iteration) — a single model, not
# the MODELS under test above. Same provider-prefix convention as MODELS.
# Not needed if MODES is just export_latest.
REASONING_MODEL="openai/gpt-5.5-2026-04-23"

# Generation knobs (scenario_builder/config.py defaults shown)
DIFFICULTY="5"
N_RETRIES="3"
N_SOL_STEPS="5"
N_TEST_STEPS="5"
N_SEC_STEPS="5"
DEBUG="false"

# Artifacts (gen_scenarios/ sits at the repo root, alongside src/ and
# baxbench's own results/); empty ARTIFACTS_DIR = scenario_builder/config.py's
# own default. EXPORT_DIR only used by MODES entries of export_latest.
ARTIFACTS_DIR=""
EXPORT_DIR=""
# ===========================================

# Fixed dependency order: generate_performance needs implementations
# generate_tests produces, export_latest needs whatever came before it, etc.
# MODES is normalized to this order regardless of how it's listed above.
CANONICAL_MODES=(generate_scenarios generate_tests generate_exploits generate_performance export_latest)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

add_arg() {
    local flag=$1
    local value=$2
    if [ -n "$value" ]; then
        ARGS+=("$flag")
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

[ -z "${MODES// }" ] && { echo "ERROR: MODES is empty." >&2; exit 1; }
for m in $MODES; do
    known=false
    for c in "${CANONICAL_MODES[@]}"; do
        [ "$m" == "$c" ] && { known=true; break; }
    done
    if [ "$known" == "false" ]; then
        echo "ERROR: unknown mode '${m}' in MODES. Known modes: ${CANONICAL_MODES[*]}" >&2
        exit 1
    fi
done

mode_requested() {
    local needle=$1
    for m in $MODES; do
        [ "$m" == "$needle" ] && return 0
    done
    return 1
}

[ -z "$ARTIFACTS_DIR" ] && ARTIFACTS_DIR="${ROOT}/gen_scenarios/artifacts"
mkdir -p "${ARTIFACTS_DIR}"
# Resolve to absolute paths: --mode build-scenarios dispatches to a subprocess
# running with cwd=src/scenario_builder/, so relative paths here would
# otherwise resolve against the wrong directory.
ARTIFACTS_DIR="$(cd "${ARTIFACTS_DIR}" && pwd)"
if [ -n "$EXPORT_DIR" ]; then
    mkdir -p "${EXPORT_DIR}"
    EXPORT_DIR="$(cd "${EXPORT_DIR}" && pwd)"
fi

run_step() {
    local mode=$1
    local scenario=$2  # empty for generate_scenarios, which takes no --scenario

    ARGS=("--mode" "build-scenarios" "--${mode}" "--path" "${ARTIFACTS_DIR}")
    add_arg "--scenario" "$scenario"
    add_arg "--export_dir" "$EXPORT_DIR"
    add_arg "--models" "$MODELS"
    add_arg "--envs" "$ENVS"
    add_arg "--reasoning_model" "$REASONING_MODEL"
    add_arg "--difficulty" "$DIFFICULTY"
    add_arg "--N_RETRIES" "$N_RETRIES"
    add_arg "--N_SOL_STEPS" "$N_SOL_STEPS"
    add_arg "--N_TEST_STEPS" "$N_TEST_STEPS"
    add_arg "--N_SEC_STEPS" "$N_SEC_STEPS"
    add_flag "--debug" "$DEBUG"

    echo "=== BaxBench scenario bootstrapping (${mode}${scenario:+ / ${scenario}}) ==="
    echo "SCENARIO:        ${scenario:-(none)}"
    echo "ARTIFACTS_DIR:   ${ARTIFACTS_DIR}"
    echo "MODELS:          ${MODELS}"
    echo "ENVS:            ${ENVS}"
    echo "REASONING_MODEL: ${REASONING_MODEL:-(none — required unless this step is export_latest)}"
    echo "Command: pipenv run python src/main.py ${ARGS[*]}"
    echo ""

    (cd "$ROOT" && pipenv run python src/main.py "${ARGS[@]}")
}

for mode in "${CANONICAL_MODES[@]}"; do
    mode_requested "$mode" || continue

    if [ "$mode" == "generate_scenarios" ]; then
        run_step "$mode" ""
        continue
    fi

    if [ -z "${SCENARIOS// }" ]; then
        echo "ERROR: mode '${mode}' requires at least one entry in SCENARIOS." >&2
        exit 1
    fi

    for scenario in $SCENARIOS; do
        run_step "$mode" "$scenario"
    done
done

echo "=== All requested modes complete: ${MODES} ==="

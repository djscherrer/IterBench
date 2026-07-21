#!/bin/bash
# BaxBench - scenario bootstrapping (AutoBaxBuilder / scenario_builder)
#
# Generates a new scenario end-to-end via `src/main.py --mode build-scenarios`,
# which dispatches straight to scenario_builder/orchestrator.py (its own
# separate CLI — see src/scenario_builder/config.py for the full flag list).
#
# Pick exactly one MODE per invocation, matching scenario_builder's own
# mutually-exclusive generation steps:
#   generate_scenarios   generate a new scenario idea + OpenAPI spec + text spec
#   generate_tests       generate + iterate functional tests (needs SCENARIO)
#   generate_exploits    generate + iterate security exploits (needs SCENARIO)
#   generate_performance generate + verify a Locust script (needs SCENARIO)
#   export_latest        promote the latest snapshot into scenarios/generated_scenarios/

set -euo pipefail

# === EDIT THESE ===========================
MODE="generate_scenarios"
SCENARIO=""

# Space-separated; empty means "use scenario_builder/config.py's own defaults".
MODELS=""
ENVS=""

# Generation knobs (scenario_builder/config.py defaults shown)
DIFFICULTY="5"
N_RETRIES="3"
N_SOL_STEPS="5"
N_TEST_STEPS="5"
N_SEC_STEPS="5"
DEBUG="false"

# Artifacts (gen_scenarios/ sits at the repo root, alongside src/ and
# baxbench's own results/); empty ARTIFACTS_DIR = scenario_builder/config.py's
# own default. EXPORT_DIR only used by MODE=export_latest.
ARTIFACTS_DIR=""
EXPORT_DIR=""
# ===========================================

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

ARGS=("--mode" "build-scenarios" "--${MODE}" "--path" "${ARTIFACTS_DIR}")

add_arg "--scenario" "$SCENARIO"
add_arg "--export_dir" "$EXPORT_DIR"
add_arg "--models" "$MODELS"
add_arg "--envs" "$ENVS"
add_arg "--difficulty" "$DIFFICULTY"
add_arg "--N_RETRIES" "$N_RETRIES"
add_arg "--N_SOL_STEPS" "$N_SOL_STEPS"
add_arg "--N_TEST_STEPS" "$N_TEST_STEPS"
add_arg "--N_SEC_STEPS" "$N_SEC_STEPS"
add_flag "--debug" "$DEBUG"

echo "=== BaxBench scenario bootstrapping (${MODE}) ==="
echo "SCENARIO:      ${SCENARIO:-(none — required for generate_tests/generate_exploits/generate_performance)}"
echo "ARTIFACTS_DIR: ${ARTIFACTS_DIR}"
echo "MODELS:        ${MODELS:-(scenario_builder/config.py default)}"
echo "ENVS:          ${ENVS:-(scenario_builder/config.py default)}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && pipenv run python src/main.py "${ARGS[@]}")

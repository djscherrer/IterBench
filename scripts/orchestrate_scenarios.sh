#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

SCENARIOS=(
  "TextWeaver_PatternRewriter"
)

ARTIFACTS_DIR="${ARTIFACTS_DIR:-${REPO_ROOT}/gen_scenarios/artifacts}"
mkdir -p "${ARTIFACTS_DIR}"
# Absolute: --mode build-scenarios dispatches to a subprocess running with
# cwd=src/scenario_builder/, so a relative path here would resolve wrong.
ARTIFACTS_DIR="$(cd "${ARTIFACTS_DIR}" && pwd)"
EXPORT_DIR="${EXPORT_DIR:-${REPO_ROOT}/src/scenarios/generated_scenarios}"

run_cmd() {
  echo
  echo "==> $*"
  "$@"
}

run_for_scenario() {
  local scenario="$1"

  if [[ ! -d "${ARTIFACTS_DIR}/${scenario}" ]]; then
    echo "ERROR: scenario artifacts directory not found: ${ARTIFACTS_DIR}/${scenario}" >&2
    exit 1
  fi

  echo
  echo "#############################################"
  echo "# Scenario: ${scenario}"
  echo "#############################################"

  # run_cmd python src/main.py --mode build-scenarios --generate_tests --path "${ARTIFACTS_DIR}" --scenario "${scenario}"
  run_cmd python src/main.py --mode build-scenarios --generate_performance --path "${ARTIFACTS_DIR}" --scenario "${scenario}"
  run_cmd python src/main.py --mode build-scenarios --generate_exploits --path "${ARTIFACTS_DIR}" --scenario "${scenario}"
  run_cmd python src/main.py --mode build-scenarios --export_latest --path "${ARTIFACTS_DIR}" --scenario "${scenario}" --export_dir "${EXPORT_DIR}"
}

echo "Running scenario_builder orchestration"
echo "- ARTIFACTS_DIR=${ARTIFACTS_DIR}"
echo "- EXPORT_DIR=${EXPORT_DIR}"
echo "- SCENARIOS=${SCENARIOS[*]}"

for scenario in "${SCENARIOS[@]}"; do
  run_for_scenario "${scenario}"
done

echo
echo "Done."

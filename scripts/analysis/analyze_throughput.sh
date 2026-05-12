#!/usr/bin/env bash
set -euo pipefail

# Analyze throughput for a single run dir or a results root.
#
# Usage:
#   bash scripts/analysis/analyze_throughput.sh


# --- 1) Target ---
# Analyze a single perf-* run dir, a sample dir, or any results subtree.
TARGET_PATH="results"

# --- 2) Analysis grouping / output identity ---
SYSTEM_CONFIG=""   # e.g. "3C-2B-1DB" (optional; inferred from perf dir when empty)
LOAD_PROFILE=""    # e.g. "stairs-1500-100-30-15" (optional; inferred from perf dir when empty)
# Optional identifier if you want multiple analyses side-by-side.
# Leave empty for the default (no extra directory layer).
ANALYSIS_ID=""

# --- 3) Throughput / SLA parameters ---
SLA_MS="300"
TRIM_S="15"

# Optional substring filter (legacy behavior): only include paths containing this string.
PROFILE_FILTER=""

# Package code lives under src/; -m needs PYTHONPATH so `distributed_bench` resolves
# (same layout as `pipenv run python src/main.py`, which adds src/ via the script path).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=()

add_arg() {
  local flag=$1
  local value=$2
  if [ -n "$value" ]; then
    ARGS+=("$flag" "$value")
  fi
}

# Defaults (can be overridden by CLI args after these)
ARGS+=("$TARGET_PATH")
add_arg "--sla" "$SLA_MS"
add_arg "--trim" "$TRIM_S"
add_arg "--system-config" "$SYSTEM_CONFIG"
add_arg "--load-profile" "$LOAD_PROFILE"
add_arg "--analysis-id" "$ANALYSIS_ID"
add_arg "--profile" "$PROFILE_FILTER"

# Append explicit CLI overrides
if [ "$#" -gt 0 ]; then
  ARGS+=("$@")
fi

CMD="pipenv run python -m distributed_bench.analysis.analyze_throughput"
for a in "${ARGS[@]}"; do
  CMD+=" $(printf "%q" "$a")"
done

echo "Executing: $CMD"
eval "$CMD"


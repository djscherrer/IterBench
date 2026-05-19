#!/bin/bash
# BaxBench - LLM-generated Kubernetes workload spec only (no deploy/bench)
#
# Prefer ./scripts/bench_k8s.sh (integrated spec gen + deploy + Locust).
# Use this script when you only want spec.yaml without benchmarking.
#
# Prerequisites:
#   1. generate + test completed for target samples (functional tests passing)
#   2. ./scripts/k8s_setup_cluster.sh (cluster Ready) if using --k8s-require-cluster
#
# Quick smoke (sample 0, force regenerate iteration-001):
#   ONLY_SAMPLES="0" K8S_ITERATION="iteration-001" FORCE="true" ./scripts/generate_k8s_spec.sh

set -euo pipefail

MODELS="anthropic/claude-opus-4-6"
ONLY_SAMPLES=""
N_SAMPLES="5"
ENVS="JavaScript-express"
SCENARIOS="BranchWeave_InteractiveStoryGraph"
TEMPERATURE="0.2"
SAFETY_PROMPT="high_performance"
BAXBENCH_LOAD_PROFILE="quick-check"

BAXBENCH_K8S_CLUSTER="baxbench-emulab"
KUBECONFIG_PATH=""
K8S_ITERATION=""
K8S_REQUIRE_CLUSTER="true"
FORCE="false"
MAX_RETRIES="3"
RESULTS_DIR=""

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

add_arg() {
  local flag=$1 value=$2
  if [ -n "$value" ]; then
    ARGS+=("$flag")
    for part in $value; do ARGS+=("$part"); done
  fi
}

add_flag() {
  if [ "$2" == "true" ]; then ARGS+=("$1"); fi
}

if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
  _kc=$(cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster import resolve_cluster_profile
import os
p = resolve_cluster_profile('${BAXBENCH_K8S_CLUSTER}')
print(os.path.expanduser(p.kubeconfig_path) if p.kubeconfig_path else '')
" 2>/dev/null | tail -n 1) || true
  if [ -n "$_kc" ]; then
    export KUBECONFIG="${KUBECONFIG_PATH:-$_kc}"
  fi
fi
[ -n "$KUBECONFIG_PATH" ] && export KUBECONFIG="$KUBECONFIG_PATH"

ARGS=("--mode" "k8s-spec-gen")
add_arg "--models" "$MODELS"
add_arg "--only_samples" "$ONLY_SAMPLES"
add_arg "--n_samples" "$N_SAMPLES"
add_arg "--envs" "$ENVS"
add_arg "--scenarios" "$SCENARIOS"
add_arg "--temperature" "$TEMPERATURE"
add_arg "--safety_prompt" "$SAFETY_PROMPT"
add_arg "--k8s-cluster" "$BAXBENCH_K8S_CLUSTER"
add_arg "--k8s-iteration" "$K8S_ITERATION"
add_flag "--force" "$FORCE"
add_arg "--max_retries" "$MAX_RETRIES"
add_arg "--results_dir" "$RESULTS_DIR"
if [ "$K8S_REQUIRE_CLUSTER" == "false" ]; then
  ARGS+=("--no-k8s-require-cluster")
fi

EXTRA_ENV=()
[ -n "$BAXBENCH_LOAD_PROFILE" ] && EXTRA_ENV+=("BAXBENCH_LOAD_PROFILE=$BAXBENCH_LOAD_PROFILE")
[ -n "$BAXBENCH_K8S_CLUSTER" ] && EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
[ -n "${KUBECONFIG:-}" ] && EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG")

echo "=== K8s spec generation (load_profile=$BAXBENCH_LOAD_PROFILE) ==="
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

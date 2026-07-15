#!/bin/bash
# BaxBench - Kubernetes iterative benchmarking
#
# Runs `python src/main.py --mode k8s-bench` for each phase:
#   1. LLM generates iterations/iteration-NNN/spec/spec.yaml (replicas, CPU/memory)
#   2. Render manifests → deploy to cluster → distributed Locust (profile load_master/workers)
#   3. Optional further phases (iteration-002+) use feedback from prior Locust run
#
# Prerequisites:
#   1. ./scripts/k8s_preflight.sh
#   2. ./scripts/k8s_setup_cluster.sh  (kubeadm + registry when profile enables it)
#   3. Docker on node0 for build + push
#
# Quick smoke (one sample, one phase):
#   ONLY_SAMPLES="0" FORCE="true" ./scripts/bench_k8s.sh
#
# Three improvement phases after the initial deploy (iteration-001..004):
#   K8S_ITERATIONS="4" ONLY_SAMPLES="0" FORCE="true" ./scripts/bench_k8s.sh
#
# Isolate a fresh iteration chain (configs + perf under sampleN/k8s-experiments/<slug>/):
#   K8S_EXPERIMENT="adaptive-may20" K8S_ITERATIONS="5" ./scripts/bench_k8s.sh
# Trajectory log: sampleN/k8s-experiments/<slug>/experiment_summary.md (updated each phase)

set -euo pipefail

# --- Experiment scope (what BaxBench task / sample to run) ---
MODELS="z-ai/glm-5.2" # deepseek/deepseek-v3.2  anthropic/claude-opus-4-6 openai/gpt-5.5-2026-04-23 anthropic/claude-opus-4-8 (temp deprecated) 
PROVIDER="openrouter"           # openai | anthropic | together_ai | openrouter | swissai | vllm
                                # required when the model prefix is not auto-detected (e.g. deepseek/…)
ONLY_SAMPLES="0"                # e.g. "0"; empty → N_SAMPLES
N_SAMPLES=""
ENVS="Python-Flask Go-net/http Rust-Actix"               # Go-net/http
SCENARIOS="Petstore Recipes ClickCount TextWeaver_WordCountDatasets BranchWeave_InteractiveStoryGraph" # Recipes LexiTally_WordCountDatasets BranchWeave_InteractiveStoryGraph TextWeaver_WordCountDatasets BranchWeave_Petstore goes to 5k users
TEMPERATURE="0.2"
SAFETY_PROMPT="high_performance"
RESULTS_DIR=""                  # empty → default results path

# --- Run configuration (k8s experiment workspace + iterative loop) ---
# Cluster profile: kubeconfig, nodes, registry, Locust hosts (see k8s_bench/cluster/profiles.py).
K8S_CLUSTER="baxbench-emulab"
# Workspace slug → results/.../sampleN/k8s-experiments/<slug>/
K8S_EXPERIMENT="results"
# Iterative loop: iteration-000 (baseline) .. iteration-NNN (N = value below)
K8S_ITERATIONS="10"
K8S_WAIT_TIMEOUT="1200"         # seconds to wait for K8s resources to become Ready
K8S_REFINEMENT="auto"           # auto | deployment | code
BASELINE_CODE_MAX_ATTEMPTS="10"
BASELINE_SPEC_MAX_ATTEMPTS="10"

# --- LLM limits ---
# Ledger: sampleN/k8s-experiments/<slug>/llm_cost_ledger.json
BAXBENCH_LLM_MAX_COST="10"      # USD; stop when estimated experiment spend exceeds this
MAX_RETRIES="3"                 # per-call LLM retry/backoff during codegen, spec, decision

# --- Bench configuration (Locust load + re-run behaviour) ---
BAXBENCH_LOAD_PROFILE=("k8s-explore-refine")  # users, spawn rate, runtime from profile registry
FORCE="false"                   # true = redo iterations even if already finished (success or failed)

# --- Execution ---
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
# Suppress per-command ssh/scp spam in 05-bench/bench.log (set to 1 to debug remoting).
export BAXBENCH_LOG_COMMANDS="${BAXBENCH_LOG_COMMANDS:-0}"

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

MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/matplotlib-${USER}}"
export MPLCONFIGDIR

if [ -n "$K8S_CLUSTER" ]; then
  _kc=$(cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster import resolve_cluster_profile
import os
p = resolve_cluster_profile('${K8S_CLUSTER}')
print(os.path.expanduser(p.kubeconfig_path) if p.kubeconfig_path else '')
" 2>/dev/null | tail -n 1) || true
  if [ -n "$_kc" ]; then
    export KUBECONFIG="$_kc"
  fi
  echo "Using K8s cluster profile: ${K8S_CLUSTER}"
fi

echo "kubectl context: $(kubectl config current-context 2>/dev/null || echo '(not configured)')"
echo "KUBECONFIG=${KUBECONFIG:-(profile default)}  experiment=${K8S_EXPERIMENT:-default}  iterations=${K8S_ITERATIONS:-1}"

BASE_ENV=()
RUN_I=0
for _model in $MODELS; do
  ARGS=("--mode" "k8s-bench")

  add_arg "--models" "$_model"
  add_arg "--provider" "$PROVIDER"
  add_arg "--only_samples" "$ONLY_SAMPLES"
  add_arg "--n_samples" "$N_SAMPLES"
  add_arg "--envs" "$ENVS"
  add_arg "--scenarios" "$SCENARIOS"
  add_arg "--temperature" "$TEMPERATURE"
  add_arg "--safety_prompt" "$SAFETY_PROMPT"
  add_arg "--results_dir" "$RESULTS_DIR"

  add_arg "--k8s-cluster" "$K8S_CLUSTER"
  ARGS+=("--k8s-experiment" "${K8S_EXPERIMENT:-default}")
  ARGS+=("--k8s-iterations" "${K8S_ITERATIONS:-1}")
  add_arg "--k8s-wait-timeout" "$K8S_WAIT_TIMEOUT"
  add_arg "--k8s-refinement" "$K8S_REFINEMENT"
  add_arg "--baseline-code-max-attempts" "$BASELINE_CODE_MAX_ATTEMPTS"
  add_arg "--baseline-spec-max-attempts" "$BASELINE_SPEC_MAX_ATTEMPTS"
  add_arg "--llm-max-cost" "$BAXBENCH_LLM_MAX_COST"
  add_arg "--max_retries" "$MAX_RETRIES"

  add_flag "--force" "$FORCE"

  for profile in "${BAXBENCH_LOAD_PROFILE[@]}"; do
    RUN_I=$((RUN_I+1))
    EXTRA_ENV=("${BASE_ENV[@]}")
    if [ -n "${KUBECONFIG:-}" ]; then
      EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG")
    fi
    PROFILE_ARGS=("${ARGS[@]}")
    if [ -n "$profile" ]; then
      PROFILE_ARGS+=("--load-profile" "$profile")
    fi
    echo ""
    echo "=== K8s bench run #$RUN_I: model='${_model}' experiment='${K8S_EXPERIMENT:-default}' load_profile='$profile' iterations=${K8S_ITERATIONS:-1} ==="
    echo "Command: pipenv run python src/main.py ${PROFILE_ARGS[*]}"
    (cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${PROFILE_ARGS[@]}")
    RC=$?
    if [ $RC -ne 0 ]; then
      echo "K8s bench run #$RUN_I failed (exit=$RC). Stopping."
      exit $RC
    fi
  done  # profile
done      # model

echo "All K8s bench runs completed."

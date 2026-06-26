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

# --- 1. Execution Targets ---
MODELS="deepseek/deepseek-v4-pro" # deepseek/deepseek-v3.2  anthropic/claude-opus-4-6 openai/gpt-5.4-2026-03-05
PROVIDER="openrouter"           # openai | anthropic | together_ai | openrouter | swissai | vllm
                                # required when the model prefix is not auto-detected (e.g. deepseek/…)
USE_OPENHANDS_MODES="false"
USE_OPENHANDS="false"
ONLY_SAMPLES="0"   # e.g. "0"; empty → N_SAMPLES
N_SAMPLES=""

# --- 2. Project Scope ---
ENVS="Python-Flask"
EXCLUDE_ENVS=""
SCENARIOS="Petstore"
EXCLUDE_SCENARIOS=""
TEMPERATURE="0.2"
SAFETY_PROMPT="high_performance"

# --- 3. Load profile (Locust shape; same registry as distributed bench) ---
BAXBENCH_LOAD_PROFILE=("k8s-goodput-plateau")

BENCH_USERS=""
BENCH_SPAWN_RATE=""
BENCH_RUN_TIME=""

# --- 4. Kubernetes settings ---
BAXBENCH_K8S_CLUSTER="baxbench-emulab"
KUBECONFIG_PATH=""              # empty = path from cluster profile
K8S_ITERATION=""                # pin one iteration; empty = use K8S_ITERATIONS
K8S_EXPERIMENT="17.6-bench-plateau-200max-4"               # e.g. adaptive-may20 → sampleN/k8s-experiments/<slug>/
K8S_ITERATIONS="20"              # phases: iteration-001 .. iteration-NNN
K8S_DEPLOY_ONLY="false"          # true = deploy+bench existing iterations only (no LLM)
K8S_WAIT_TIMEOUT="600"
# Locust runs on profile load_master/workers; backend exposed via NodePort
K8S_AUTO_INIT="false"           # only used with K8S_DEPLOY_ONLY=true
K8S_REFINEMENT="auto"               # auto | deployment | code

# Baseline (iteration-000) code source.
#   reuse      = use sampleN/code/ from --mode generate; require its FTs to pass (default)
#   regenerate = LLM-generate code with the current prompt into iteration-000-baseline/
#                02-code/code/, validate with functional tests, retry on failure
#                (failed attempts preserved under 02-code/attempts/<NNN>/).
BASELINE_CODE_MAX_ATTEMPTS="5"

# Baseline (iteration-000) spec generation attempt cap. Each attempt is one
# LLM call + static validation + deploy probe; failures are preserved under
# iteration-000-baseline/03-spec/attempts/<NNN>/. Refinement iterations
# (001+) always use a single spec attempt — recovery happens via subsequent
# iterations, not per-iteration retries.
BASELINE_SPEC_MAX_ATTEMPTS="5"

# --- LLM cost tracking (estimated; see sampleN/k8s-experiments/<slug>/llm_cost_ledger.json) ---
BAXBENCH_LLM_MAX_COST="10"            # e.g. "10.00" — stop when estimated experiment LLM spend exceeds this (USD)


# --- 5. Bench configuration ---
TIMEOUT="600"
FORCE="false"
MAX_CONCURRENT_RUNS=""
PORT="5001"
MAX_RETRIES="3"

# --- 6. Global settings ---
RESULTS_DIR=""
PLOT_AFTER_BENCH="false"

# --- Execution ---
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

_openhands_modes="${USE_OPENHANDS_MODES:-$USE_OPENHANDS}"
if [ -z "$_openhands_modes" ]; then
  _openhands_modes="false"
fi

MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/matplotlib-${USER}}"
export MPLCONFIGDIR

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
  echo "Using K8s cluster profile: ${BAXBENCH_K8S_CLUSTER}"
fi

if [ -n "$KUBECONFIG_PATH" ]; then
  export KUBECONFIG="$KUBECONFIG_PATH"
fi

echo "kubectl context: $(kubectl config current-context 2>/dev/null || echo '(not configured)')"
echo "KUBECONFIG=${KUBECONFIG:-<default>}  experiment=${K8S_EXPERIMENT:-<legacy>}  iterations=${K8S_ITERATIONS}  spec_gen=${K8S_SPEC_GEN}"

BASE_ENV=()
RUN_I=0
for _model in $MODELS; do
  for _openhands in $_openhands_modes; do
    ARGS=("--mode" "k8s-bench")

    add_arg "--models" "$_model"
    add_arg "--provider" "$PROVIDER"
    add_flag "--use_openhands" "$_openhands"
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

    add_arg "--k8s-cluster" "$BAXBENCH_K8S_CLUSTER"
    add_arg "--k8s-iteration" "$K8S_ITERATION"
    add_arg "--k8s-experiment" "$K8S_EXPERIMENT"
    add_arg "--k8s-iterations" "$K8S_ITERATIONS"
    add_arg "--k8s-wait-timeout" "$K8S_WAIT_TIMEOUT"
    add_arg "--k8s-refinement" "$K8S_REFINEMENT"
    add_arg "--load-profile" "$profile"
    add_arg "--baseline-code-max-attempts" "$BASELINE_CODE_MAX_ATTEMPTS"
    add_arg "--baseline-spec-max-attempts" "$BASELINE_SPEC_MAX_ATTEMPTS"
    add_arg "--llm-max-cost" "$BAXBENCH_LLM_MAX_COST"
    add_arg "--max_retries" "$MAX_RETRIES"
    if [ "$K8S_DEPLOY_ONLY" == "true" ]; then
      ARGS+=("--deploy-only")
    fi
    if [ "$K8S_AUTO_INIT" == "true" ]; then
      ARGS+=("--k8s-auto-init")
    fi

    add_arg "--timeout" "$TIMEOUT"
    add_arg "--max_concurrent_runs" "$MAX_CONCURRENT_RUNS"
    add_flag "--force" "$FORCE"
    add_arg "--results_dir" "$RESULTS_DIR"
    add_arg "--port" "$PORT"

    if [ "$PLOT_AFTER_BENCH" == "true" ]; then
        ARGS+=("--plot-after-bench")
    fi

    for profile in "${BAXBENCH_LOAD_PROFILE[@]}"; do
      RUN_I=$((RUN_I+1))
      EXTRA_ENV=("${BASE_ENV[@]}")
      if [ -n "${KUBECONFIG:-}" ]; then
        EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG")
      fi
      echo ""
      echo "=== K8s iterative bench run #$RUN_I: model='${_model}' openhands='${_openhands}' load_profile='$profile' iterations=$K8S_ITERATIONS ==="
      echo "Command: pipenv run python src/main.py ${ARGS[*]}"
      (cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")
      RC=$?
      if [ $RC -ne 0 ]; then
        echo "K8s bench run #$RUN_I failed (exit=$RC). Stopping."
        exit $RC
      fi
    done  # profile
  done    # openhands
done      # model

echo "All K8s bench runs completed."

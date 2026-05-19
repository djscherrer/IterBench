#!/bin/bash
# BaxBench - Kubernetes benchmarking mode
#
# Runs `python src/main.py --mode k8s-bench`: deploy workloads from
# sampleN/k8s_configs/<iteration>/spec.yaml, then Locust via kubectl port-forward.
#
# Prerequisites:
#   1. generate + test already ran for the samples you benchmark
#   2. ./scripts/k8s_setup_cluster.sh
#   3. ./scripts/k8s_setup_registry.sh  (once; images push/pull via node0:5000)
#   4. Docker on node0 for build + push
#
# Quick smoke (one sample):
#   ONLY_SAMPLES="0" FORCE="true" ./scripts/bench_k8s.sh

set -euo pipefail

# --- 1. Execution Targets ---
MODELS="anthropic/claude-opus-4-6"
USE_OPENHANDS_MODES="false"
USE_OPENHANDS=""
ONLY_SAMPLES="${ONLY_SAMPLES:-}"   # e.g. "0"; empty → N_SAMPLES
N_SAMPLES="${N_SAMPLES:-5}"

# --- 2. Project Scope ---
ENVS="JavaScript-express"
EXCLUDE_ENVS=""
SCENARIOS="BranchWeave_InteractiveStoryGraph"
EXCLUDE_SCENARIOS=""
TEMPERATURE="0.2"
SAFETY_PROMPT="high_performance"

# --- 3. Load profile (Locust shape; same registry as distributed bench) ---
BAXBENCH_LOAD_PROFILE=("quick-check")

BENCH_USERS=""
BENCH_SPAWN_RATE=""
BENCH_RUN_TIME=""

# --- 4. Kubernetes settings ---
BAXBENCH_K8S_CLUSTER="baxbench-emulab"
KUBECONFIG_PATH=""              # empty = path from cluster profile
K8S_ITERATION=""                # e.g. "iteration-001"; empty = all iterations or auto-init
K8S_WAIT_TIMEOUT="300"
K8S_LOCAL_PORT=""               # fixed local port for port-forward; empty = ephemeral
K8S_AUTO_INIT="true"
# Registry push is automatic when profile has registry_enabled=true (baxbench-emulab)
# Fail fast if cluster API / nodes are not healthy (same checks as preflight cluster phase)
K8S_REQUIRE_CLUSTER="true"
K8S_NODE_HOSTS="node0 node2 node3 node4 node5"

# --- 5. Bench configuration ---
TIMEOUT="600"
FORCE="${FORCE:-false}"
MAX_CONCURRENT_RUNS=""
PORT="5001"

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
from k8s_bench.cluster_configs import resolve_cluster_profile
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
echo "KUBECONFIG=${KUBECONFIG:-<default>}  require_cluster=${K8S_REQUIRE_CLUSTER}"

BASE_ENV=()
RUN_I=0
for _model in $MODELS; do
  for _openhands in $_openhands_modes; do
    ARGS=("--mode" "k8s-bench")

    add_arg "--models" "$_model"
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
    add_arg "--k8s-wait-timeout" "$K8S_WAIT_TIMEOUT"
    add_arg "--k8s-local-port" "$K8S_LOCAL_PORT"
    if [ "$K8S_REQUIRE_CLUSTER" == "false" ]; then
      ARGS+=("--no-k8s-require-cluster")
    fi
    if [ "$K8S_AUTO_INIT" == "false" ]; then
      ARGS+=("--no-k8s-auto-init")
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
      if [ -n "$profile" ]; then
        EXTRA_ENV+=("BAXBENCH_LOAD_PROFILE=$profile")
      fi
      if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
        EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
      fi
      if [ -n "$K8S_ITERATION" ]; then
        EXTRA_ENV+=("BAXBENCH_K8S_ITERATION=$K8S_ITERATION")
      fi
      if [ -n "${KUBECONFIG:-}" ]; then
        EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG")
      fi
      if [ -n "${K8S_WORKER_HOSTS:-}" ]; then
        EXTRA_ENV+=("BAXBENCH_K8S_WORKER_HOSTS=$K8S_WORKER_HOSTS")
      fi
      if [ -n "${K8S_NODE_HOSTS:-}" ]; then
        EXTRA_ENV+=("BAXBENCH_K8S_NODE_HOSTS=$K8S_NODE_HOSTS")
      fi

      echo ""
      echo "=== K8s bench run #$RUN_I: model='${_model}' openhands='${_openhands}' load_profile='$profile' ==="
      echo "Command: pipenv run python src/main.py ${ARGS[*]}"
      (cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")
      RC=$?
      if [ $RC -ne 0 ]; then
        echo "K8s bench run #$RUN_I failed (exit=$RC). Stopping."
        exit $RC
      fi
    done
  done
done

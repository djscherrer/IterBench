#!/bin/bash
# BaxBench - Kubernetes preflight
#
#   1. THIS SCRIPT  — install/check packages (K8S_INSTALL_PREREQUISITES=true)
#   2. ./scripts/k8s_setup_cluster.sh — kubeadm init + join workers
#   3. ./scripts/k8s_setup_registry.sh — private image registry on node0:5000
#   4. THIS SCRIPT  — K8S_SKIP_CLUSTER_CHECKS=false to verify the cluster (optional)
#
# Usage: edit variables below, then ./scripts/k8s_preflight.sh

set -euo pipefail

# --- 1. Cluster access ---
BAXBENCH_K8S_CLUSTER="baxbench-emulab"
KUBECONFIG_PATH=""

# --- 2. Kubernetes cluster members (control-plane + workers) ---
# node0 = control-plane + BaxBench; node2–5 = workers
K8S_NODE_HOSTS="node0 node2 node3 node4 node5"

# --- 3. Locust-only hosts (never kubeadm-join) ---
K8S_LOAD_HOSTS="node1"

# --- 4. Preflight behaviour ---
# First run: true to install packages on all hosts above. Later runs: false (check only).
K8S_INSTALL_PREREQUISITES="true"

# true until after kubeadm init + kubeconfig copied; then false
K8S_SKIP_CLUSTER_CHECKS="false"

# Optional: Kubernetes apt channel (pkgs.k8s.io), e.g. v1.29
# BAXBENCH_K8S_APT_CHANNEL="v1.29"

# --- Execution ---
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=("--mode" "k8s-preflight")

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

if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    add_arg "--k8s-cluster" "$BAXBENCH_K8S_CLUSTER"
fi
add_arg "--k8s-node-hosts" "$K8S_NODE_HOSTS"
add_arg "--k8s-load-hosts" "$K8S_LOAD_HOSTS"
add_flag "--k8s-install-prerequisites" "$K8S_INSTALL_PREREQUISITES"
add_flag "--k8s-skip-cluster-checks" "$K8S_SKIP_CLUSTER_CHECKS"

EXTRA_ENV=()
if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
fi
# Host lists are passed only via --k8s-node-hosts / --k8s-load-hosts (not duplicated in env).
if [ "$K8S_INSTALL_PREREQUISITES" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_AUTO_INSTALL_REMOTE_DEPS=1")
fi
EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=0")

_resolve_kubeconfig() {
    if [ -n "$KUBECONFIG_PATH" ]; then
        echo "$KUBECONFIG_PATH"
        return
    fi
    if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
        (cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster_configs import resolve_cluster_profile
import os
p = resolve_cluster_profile('${BAXBENCH_K8S_CLUSTER}')
path = p.kubeconfig_path
print(os.path.expanduser(path) if path else '')
" 2>/dev/null) || true
    fi
}

_KUBECONFIG_RESOLVED=$(_resolve_kubeconfig | tail -n 1)
if [ -n "$_KUBECONFIG_RESOLVED" ]; then
    export KUBECONFIG="$_KUBECONFIG_RESOLVED"
    EXTRA_ENV+=("KUBECONFIG=$_KUBECONFIG_RESOLVED")
fi

echo "=== BaxBench k8s-preflight ==="
echo "Phase 1: install/check SSH hosts (install=${K8S_INSTALL_PREREQUISITES})"
echo "  K8s nodes: ${K8S_NODE_HOSTS:-<none>}"
echo "  Locust:    ${K8S_LOAD_HOSTS:-<none>}"
echo "Next: ./scripts/k8s_setup_cluster.sh (after packages OK)"
echo "kubectl:   skip_cluster_checks=${K8S_SKIP_CLUSTER_CHECKS} KUBECONFIG=${KUBECONFIG:-<default>}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

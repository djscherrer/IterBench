#!/bin/bash
# BaxBench - Kubernetes cluster bootstrap (kubeadm init + worker join)
#
# Run FROM node0 (control-plane) AFTER ./scripts/k8s_preflight.sh succeeds.
#
# Topology: src/k8s_bench/cluster/profiles.py (control_node, worker_nodes)
# Select profile: BAXBENCH_K8S_CLUSTER below.

set -euo pipefail

BAXBENCH_K8S_CLUSTER="baxbench-emulab"
KUBECONFIG_PATH=""

# kubeadm / CNI (not host topology)
K8S_POD_NETWORK_CIDR="10.244.0.0/16"
K8S_CNI="flannel"
K8S_SKIP_CNI="false"
K8S_WAIT_TIMEOUT="600"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=("--mode" "k8s-setup-cluster")

add_arg() {
    local flag=$1
    local value=$2
    if [ -n "$value" ]; then
        ARGS+=("$flag" "$value")
    fi
}

add_flag() {
    if [ "$2" == "true" ]; then
        ARGS+=("$1")
    fi
}

if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    ARGS+=("--k8s-cluster" "$BAXBENCH_K8S_CLUSTER")
fi
add_arg "--k8s-pod-network-cidr" "$K8S_POD_NETWORK_CIDR"
add_arg "--k8s-cni" "$K8S_CNI"
add_arg "--k8s-wait-timeout" "$K8S_WAIT_TIMEOUT"
add_flag "--k8s-skip-cni" "$K8S_SKIP_CNI"

EXTRA_ENV=()
if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
fi
EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=0")

if [ -n "$KUBECONFIG_PATH" ]; then
    export KUBECONFIG="$KUBECONFIG_PATH"
    EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG_PATH")
elif [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    _kc=$(cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster import resolve_cluster_profile
import os
p = resolve_cluster_profile('${BAXBENCH_K8S_CLUSTER}')
print(os.path.expanduser(p.kubeconfig_path) if p.kubeconfig_path else '')
" 2>/dev/null | tail -n 1) || true
    if [ -n "$_kc" ]; then
        export KUBECONFIG="$_kc"
        EXTRA_ENV+=("KUBECONFIG=$_kc")
    fi
fi

echo "=== BaxBench k8s-setup-cluster ==="
echo "Profile:   ${BAXBENCH_K8S_CLUSTER} (control/workers in profiles.py)"
echo "Pod CIDR:  ${K8S_POD_NETWORK_CIDR}  CNI: ${K8S_CNI}"
echo "Kubeconfig: ${KUBECONFIG:-<from profile>}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

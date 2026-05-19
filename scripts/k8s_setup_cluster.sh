#!/bin/bash
# BaxBench - Kubernetes cluster bootstrap (kubeadm init + worker join)
#
# Run FROM node0 (control-plane) AFTER ./scripts/k8s_preflight.sh succeeds.
#
#   1. ./scripts/k8s_preflight.sh     # install containerd, kubeadm, …
#   2. ./scripts/k8s_setup_cluster.sh # kubeadm init on node0, join node2–5
#   3. ./scripts/k8s_setup_registry.sh
#   4. ./scripts/k8s_preflight.sh     # K8S_SKIP_CLUSTER_CHECKS=false (optional)
#
# Edit variables below, then run from the repo root.

set -euo pipefail

# --- Cluster profile (kubeconfig destination) ---
BAXBENCH_K8S_CLUSTER="baxbench-emulab"
KUBECONFIG_PATH=""              # empty = profile default (/tmp/dscherre/.kube/...)

# --- Topology ---
K8S_CONTROL_PLANE_HOST="node0"
K8S_WORKER_HOSTS="node2 node3 node4 node5"
# Optional fallback if workers empty: all K8S_NODE_HOSTS except control-plane
K8S_NODE_HOSTS="node0 node2 node3 node4 node5"

# --- kubeadm / CNI ---
K8S_POD_NETWORK_CIDR="10.244.0.0/16"   # must match Flannel default
K8S_CNI="flannel"
K8S_SKIP_CNI="false"
K8S_WAIT_TIMEOUT="600"

# --- Execution ---
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=("--mode" "k8s-setup-cluster")

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
add_arg "--k8s-control-plane" "$K8S_CONTROL_PLANE_HOST"
add_arg "--k8s-worker-hosts" "$K8S_WORKER_HOSTS"
add_arg "--k8s-node-hosts" "$K8S_NODE_HOSTS"
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
echo "Control-plane: ${K8S_CONTROL_PLANE_HOST}"
echo "Workers:       ${K8S_WORKER_HOSTS}"
echo "Pod CIDR:      ${K8S_POD_NETWORK_CIDR}  CNI: ${K8S_CNI}"
echo "Kubeconfig:    ${KUBECONFIG:-<from profile>}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

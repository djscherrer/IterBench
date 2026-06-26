#!/bin/bash
# BaxBench - Kubernetes lab bootstrap (kubeadm + optional private registry)
#
# Run FROM node0 (control-plane) AFTER ./scripts/k8s_preflight.sh succeeds.
# Installs the cluster, then configures the image registry when the profile has
# registry_enabled=true (e.g. baxbench-emulab).
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
# Set true to skip registry even when the profile has registry_enabled=true
K8S_SKIP_REGISTRY="false"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

add_arg() {
    local -n _args=$1
    local flag=$2
    local value=$3
    if [ -n "$value" ]; then
        _args+=("$flag" "$value")
    fi
}

add_flag() {
    local -n _args=$1
    if [ "$3" == "true" ]; then
        _args+=("$2")
    fi
}

_cluster_args() {
    local -n _out=$1
    _out=("--mode" "k8s-setup-cluster")
    if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
        _out+=("--k8s-cluster" "$BAXBENCH_K8S_CLUSTER")
    fi
    add_arg _out "--k8s-pod-network-cidr" "$K8S_POD_NETWORK_CIDR"
    add_arg _out "--k8s-cni" "$K8S_CNI"
    add_arg _out "--k8s-wait-timeout" "$K8S_WAIT_TIMEOUT"
    add_flag _out "--k8s-skip-cni" "$K8S_SKIP_CNI"
}

_registry_args() {
    local -n _out=$1
    _out=("--mode" "k8s-setup-registry")
    if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
        _out+=("--k8s-cluster" "$BAXBENCH_K8S_CLUSTER")
    fi
}

_profile_registry_enabled() {
    if [ -z "$BAXBENCH_K8S_CLUSTER" ]; then
        echo "false"
        return
    fi
    (cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster import resolve_cluster_profile
p = resolve_cluster_profile('${BAXBENCH_K8S_CLUSTER}')
print('true' if p.registry_enabled else 'false')
" 2>/dev/null | tail -n 1) || echo "false"
}

EXTRA_ENV=()
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

_run_mode() {
    local label=$1
    shift
    echo ""
    echo "=== BaxBench ${label} ==="
    echo "Profile:    ${BAXBENCH_K8S_CLUSTER} (hosts in profiles.py)"
    echo "Kubeconfig: ${KUBECONFIG:-<from profile>}"
    echo "Command:    pipenv run python src/main.py $*"
    echo ""
    (cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "$@")
}

CLUSTER_ARGS=()
_cluster_args CLUSTER_ARGS
echo "=== BaxBench k8s lab setup ==="
echo "Pod CIDR: ${K8S_POD_NETWORK_CIDR}  CNI: ${K8S_CNI}"
_run_mode "k8s-setup-cluster" "${CLUSTER_ARGS[@]}"

_REGISTRY_ENABLED=$(_profile_registry_enabled)
if [ "$K8S_SKIP_REGISTRY" == "true" ]; then
    echo ""
    echo "Skipping registry (K8S_SKIP_REGISTRY=true)."
elif [ "$_REGISTRY_ENABLED" != "true" ]; then
    echo ""
    echo "Skipping registry (profile ${BAXBENCH_K8S_CLUSTER} has registry_enabled=false)."
else
    REGISTRY_ARGS=()
    _registry_args REGISTRY_ARGS
    _run_mode "k8s-setup-registry" "${REGISTRY_ARGS[@]}"
fi

echo ""
echo "Lab setup complete."

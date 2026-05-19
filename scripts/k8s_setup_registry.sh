#!/bin/bash
# BaxBench - Private Docker registry for the lab cluster
#
# Run ONCE on node0 (control-plane), AFTER k8s_setup_cluster.sh:
#
#   1. ./scripts/k8s_preflight.sh
#   2. ./scripts/k8s_setup_cluster.sh
#   3. ./scripts/k8s_setup_registry.sh   # this script
#   4. ./scripts/bench_k8s.sh
#
# Starts registry:2 on port 5000 and configures containerd on all cluster nodes
# to pull from it. BaxBench then docker push + imagePullPolicy: IfNotPresent.

set -euo pipefail

BAXBENCH_K8S_CLUSTER="baxbench-emulab"
K8S_CONTROL_PLANE_HOST="node0"
K8S_NODE_HOSTS="node0 node2 node3 node4 node5"
K8S_REGISTRY_HOST=""            # empty = auto-detect control-plane IP
K8S_REGISTRY_PORT="5000"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=("--mode" "k8s-setup-registry")

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

if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    add_arg "--k8s-cluster" "$BAXBENCH_K8S_CLUSTER"
fi
add_arg "--k8s-control-plane" "$K8S_CONTROL_PLANE_HOST"
add_arg "--k8s-node-hosts" "$K8S_NODE_HOSTS"
add_arg "--k8s-registry-host" "$K8S_REGISTRY_HOST"
add_arg "--k8s-registry-port" "$K8S_REGISTRY_PORT"

EXTRA_ENV=()
if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
fi
EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=0")

echo "=== BaxBench k8s-setup-registry ==="
echo "Control-plane: ${K8S_CONTROL_PLANE_HOST}"
echo "Cluster nodes: ${K8S_NODE_HOSTS}"
echo "Registry:      ${K8S_REGISTRY_HOST:-<auto>}:${K8S_REGISTRY_PORT}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

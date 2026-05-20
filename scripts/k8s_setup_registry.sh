#!/bin/bash
# BaxBench - Private Docker registry for the lab cluster
#
# Run ONCE on node0 (control-plane), AFTER k8s_setup_cluster.sh.
# Topology: src/k8s_bench/cluster/profiles.py (registry on control_node)

set -euo pipefail

BAXBENCH_K8S_CLUSTER="baxbench-emulab"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=("--mode" "k8s-setup-registry")

if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    ARGS+=("--k8s-cluster" "$BAXBENCH_K8S_CLUSTER")
fi

EXTRA_ENV=()
if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
fi
EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=0")

echo "=== BaxBench k8s-setup-registry ==="
echo "Profile: ${BAXBENCH_K8S_CLUSTER} (registry settings in profiles.py)"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

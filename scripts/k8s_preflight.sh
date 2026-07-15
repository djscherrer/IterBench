#!/bin/bash
# BaxBench - Kubernetes preflight
#
# Prerequisites checklist (run from node0 before the first preflight):
#   [ ] Passwordless SSH to every host in profiles.py (control, workers, Locust)
#   [ ] Host keys in ~/.ssh/known_hosts — preflight uses non-interactive SSH;
#       on a fresh/wiped cluster run once (edit hostnames to match your profile):
#         ssh-keyscan -H node0 node1 node2 node3 node4 node5 node6 node7 node8 >> ~/.ssh/known_hosts
#   [ ] kubectl on node0 PATH (apt install; not a pip package)
#   [ ] pipenv + Python 3.12; pipenv install in repo root
#
# Run order:
#   1. THIS SCRIPT  — install/check packages (K8S_INSTALL_PREREQUISITES=true)
#      First cluster bootstrap: K8S_SKIP_CLUSTER_CHECKS=true (no kubeconfig yet)
#   2. ./scripts/k8s_setup_cluster.sh — kubeadm init + join workers + registry
#   3. THIS SCRIPT  — K8S_SKIP_CLUSTER_CHECKS=false to verify the cluster (optional)
#
# Topology: edit K8S_CLUSTER_REGISTRY in src/k8s_bench/cluster/profiles.py
# Select profile: BAXBENCH_K8S_CLUSTER below.

set -euo pipefail

# --- Cluster profile (single selector; hosts live in profiles.py) ---
BAXBENCH_K8S_CLUSTER="baxbench-emulab"

# --- Preflight behaviour ---
K8S_INSTALL_PREREQUISITES="true"
K8S_SKIP_CLUSTER_CHECKS="false"

# Optional: Kubernetes apt channel (pkgs.k8s.io), e.g. v1.29
# BAXBENCH_K8S_APT_CHANNEL="v1.29"

# --- Execution ---
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=("--mode" "k8s-preflight")

add_flag() {
    if [ "$2" == "true" ]; then
        ARGS+=("$1")
    fi
}

if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    ARGS+=("--k8s-cluster" "$BAXBENCH_K8S_CLUSTER")
fi
add_flag "--k8s-install-prerequisites" "$K8S_INSTALL_PREREQUISITES"
add_flag "--k8s-skip-cluster-checks" "$K8S_SKIP_CLUSTER_CHECKS"

EXTRA_ENV=()
if [ -n "$BAXBENCH_K8S_CLUSTER" ]; then
    EXTRA_ENV+=("BAXBENCH_K8S_CLUSTER=$BAXBENCH_K8S_CLUSTER")
fi
if [ "$K8S_INSTALL_PREREQUISITES" == "true" ]; then
    EXTRA_ENV+=("BAXBENCH_AUTO_INSTALL_REMOTE_DEPS=1")
fi
EXTRA_ENV+=("BAXBENCH_LOG_COMMANDS=0")

_resolve_kubeconfig() {
    if [ -z "$BAXBENCH_K8S_CLUSTER" ]; then
        return
    fi
    (cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster import resolve_cluster_profile
import os
p = resolve_cluster_profile('${BAXBENCH_K8S_CLUSTER}')
path = p.kubeconfig_path
print(os.path.expanduser(path) if path else '')
" 2>/dev/null) || true
}

_KUBECONFIG_RESOLVED=$(_resolve_kubeconfig | tail -n 1)
if [ -n "$_KUBECONFIG_RESOLVED" ]; then
    export KUBECONFIG="$_KUBECONFIG_RESOLVED"
    EXTRA_ENV+=("KUBECONFIG=$_KUBECONFIG_RESOLVED")
fi

echo "=== BaxBench k8s-preflight ==="
echo "Profile:   ${BAXBENCH_K8S_CLUSTER} (hosts in src/k8s_bench/cluster/profiles.py)"
echo "Install:   ${K8S_INSTALL_PREREQUISITES}"
echo "Next: ./scripts/k8s_setup_cluster.sh (after packages OK)"
echo "kubectl:   skip_cluster_checks=${K8S_SKIP_CLUSTER_CHECKS} KUBECONFIG=${KUBECONFIG:-(profile default)}"
echo "Command: pipenv run python src/main.py ${ARGS[*]}"
echo ""

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# cleanup_docker_data.sh
# Removes local .tar files under a results directory and performs
# aggressive Docker cleanup on a list of remote hosts.
#
# Usage:
#   ./scripts/distributed_bench/cleanup_docker_data.sh [--preserve-volumes] [RESULTS_DIR] host1 host2 ...
# Or set hosts via env var:
#   BAXBENCH_CLEANUP_HOSTS="host1 host2" ./scripts/distributed_bench/cleanup_docker_data.sh
#
# By default this will remove containers, images, networks, volumes and builder cache
# on the remote hosts. Use --preserve-volumes to skip volume pruning.

RESULTS_DIR="${REPO_ROOT}/results"
PRESERVE_VOLUMES=0

usage() {
  cat <<EOF
Usage: $0 [--preserve-volumes] [RESULTS_DIR] host1 host2 ...
       Or set BAXBENCH_CLEANUP_HOSTS="host1 host2" and pass no hosts.

Options:
  --preserve-volumes   Do not prune Docker volumes on remote hosts.
EOF
  exit 1
}

if [ "$#" -gt 0 ]; then
  case "$1" in
    --preserve-volumes)
      PRESERVE_VOLUMES=1
      shift
      ;;
    --help|-h)
      usage
      ;;
  esac
fi

if [ "$#" -gt 0 ] && [[ ! "$1" =~ ^- ]]; then
  RESULTS_DIR="$1"
  if [[ "$RESULTS_DIR" != /* ]]; then
    RESULTS_DIR="$REPO_ROOT/$RESULTS_DIR"
  fi
  shift
fi

# Determine hosts: command-line args or env var
if [ "$#" -gt 0 ]; then
  HOSTS=("$@")
elif [ -n "${BAXBENCH_CLEANUP_HOSTS:-}" ]; then
  # split on whitespace
  read -r -a HOSTS <<< "$BAXBENCH_CLEANUP_HOSTS"
else
  echo "No hosts provided. Pass hosts as arguments or set BAXBENCH_CLEANUP_HOSTS."
  usage
fi

echo "Results dir: $RESULTS_DIR"
echo "Preserve volumes: $PRESERVE_VOLUMES"
echo "Hosts: ${HOSTS[*]}"

# 1) remove local .tar files under results dir (recursively)
if [ -d "$RESULTS_DIR" ]; then
  echo "Looking for .tar files under $RESULTS_DIR ..."
  find "$RESULTS_DIR" -type f -name '*.tar' -print -exec rm -f {} \;
  echo "Local .tar cleanup done."
else
  echo "Results dir $RESULTS_DIR does not exist; skipping local cleanup."
fi

# 2) remote cleanup script to run on each host (read via stdin)
REMOTE_SCRIPT=$(cat <<'REMOTE_EOF'
set -euo pipefail
echo "=== remote cleanup: $(hostname) ==="
docker_sock="/run/user/$(id -u)/docker.sock"

# Helper to attempt a Docker command with either sudo (rootful) or rootless socket.
run_with_docker() {
  cmd="$*"
  # Prefer sudo if available and permitted
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    if sudo -n docker info >/dev/null 2>&1; then
      echo "Using sudo docker"
      sudo -n bash -lc "$cmd"
      return $?
    fi
  fi

  # Try direct docker (may work if user has access)
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "Using direct docker"
    bash -lc "$cmd"
    return $?
  fi

  # Fallback to rootless socket
  if [ -S "$docker_sock" ]; then
    echo "Trying rootless docker socket $docker_sock"
    DOCKER_HOST="unix://$docker_sock" bash -lc "$cmd"
    return $?
  fi

  echo "ERROR: docker not available (no sudo access, no direct docker, no rootless socket)" >&2
  return 2
}

# remove all containers (stopped or running)
echo "Removing containers..."
run_with_docker 'ids=$(docker ps -aq || true); if [ -n "$ids" ]; then docker rm -f $ids || true; fi' || true

# aggressive prune: images, networks, builder cache (and optionally volumes)
echo "Pruning system (images, networks, build cache) ..."
PRUNE_CMD="docker system prune -af"
if [ "__PRESERVE_VOLUMES__" -eq 0 ]; then
  PRUNE_CMD="$PRUNE_CMD --volumes"
else
  echo "Preserving volumes as requested."
fi

# attempt builder prune first (may not be supported everywhere)
run_with_docker 'if docker builder prune -af >/dev/null 2>&1; then :; elif docker buildx prune -af >/dev/null 2>&1; then :; else echo "builder prune unsupported"; fi' || true

run_with_docker "$PRUNE_CMD" || true

echo "Remote cleanup complete for $(hostname)"
REMOTE_EOF
)

# Replace placeholder for preserve volumes flag (0/1)
if [ "$PRESERVE_VOLUMES" -eq 1 ]; then
  REMOTE_SCRIPT="${REMOTE_SCRIPT//__PRESERVE_VOLUMES__/1}"
else
  REMOTE_SCRIPT="${REMOTE_SCRIPT//__PRESERVE_VOLUMES__/0}"
fi

for h in "${HOSTS[@]}"; do
  echo "=== Cleaning host: $h ==="
  # stream script to remote via ssh (avoids quoting headaches)
  ssh -o BatchMode=yes "$h" 'bash -s' <<'SSH_EOF'
$REMOTE_SCRIPT
SSH_EOF
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "Warning: cleanup on $h exited with code $rc"
  else
    echo "Cleanup succeeded on $h"
  fi
done

echo "All done."


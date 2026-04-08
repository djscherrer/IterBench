#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Configuration (edit these defaults)
###############################################################################

# Default hosts to clean when no CLI args are provided.
# Example:
#   DEFAULT_HOSTS=(r630-02 r630-03 r630-04 r630-05 r630-08)
DEFAULT_HOSTS=(r630-02 r630-03 r630-04 r630-05 r630-08)

# What to clean
CLEAN_TUNNELS_DEFAULT="true"     # kills BaxBench SSH port-forward processes
CLEAN_CONTAINERS_DEFAULT="true"  # removes docker containers named baxbench-*

usage() {
  cat <<'EOF'
remote_cleanup.sh - clean up BaxBench remote artifacts

Usage:
  scripts/remote_cleanup.sh                 # uses DEFAULT_HOSTS
  scripts/remote_cleanup.sh host1 host2 ... # overrides DEFAULT_HOSTS

Examples:
  scripts/remote_cleanup.sh
  scripts/remote_cleanup.sh r630-02 r630-03 r630-04 r630-05 r630-08

What it does (default):
  - Kills BaxBench SSH tunnel processes on each host (port forwards)
  - Removes ONLY docker containers whose name starts with "baxbench-"
  - Prints any remaining listeners for relevant ports

Dangerous mode (NOT recommended):
  - If you set BAXBENCH_REMOTE_CLEANUP_ALL=1, it will remove ALL docker containers on each host.
    Use only on dedicated benchmark machines.

Env vars:
  - BAXBENCH_REMOTE_CLEANUP_ALL=1   (dangerous) remove ALL containers, not just baxbench-*
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ALL="${BAXBENCH_REMOTE_CLEANUP_ALL:-0}"

hosts=( "${DEFAULT_HOSTS[@]}" )
clean_tunnels="$CLEAN_TUNNELS_DEFAULT"
clean_containers="$CLEAN_CONTAINERS_DEFAULT"

# Positional args override DEFAULT_HOSTS
if [[ "$#" -gt 0 ]]; then
  hosts=( "$@" )
fi

if [[ "${#hosts[@]}" -lt 1 ]]; then
  usage
  exit 2
fi

# Deduplicate while preserving order
deduped=()
for h in "${hosts[@]}"; do
  [[ -n "$h" ]] || continue
  seen=0
  for x in "${deduped[@]}"; do
    [[ "$x" == "$h" ]] && { seen=1; break; }
  done
  [[ "$seen" == "0" ]] && deduped+=( "$h" )
done
hosts=( "${deduped[@]}" )

for h in "${hosts[@]}"; do
  echo "=== ${h} ==="
  ssh "${h}" 'bash -lc '"'"'
set -euo pipefail

clean_tunnels="'"${clean_tunnels}"'"
clean_containers="'"${clean_containers}"'"

if [[ "$clean_tunnels" == "true" ]]; then
  # Kill BaxBench SSH tunnels (port forwards) on this host
  pkill -f "ssh .* -N -L 0\.0\.0\.0:1700[0-9]:" 2>/dev/null || true
  pkill -f "ssh .* -N -L 0\.0\.0\.0:1543[0-9]:" 2>/dev/null || true
fi

if [[ "$clean_containers" == "true" ]]; then
  # Remove containers
  if [[ "'"${ALL}"'" == "1" ]]; then
    echo "WARNING: removing ALL docker containers (BAXBENCH_REMOTE_CLEANUP_ALL=1)"
    docker ps -aq | xargs -r docker rm -f
  else
    docker ps -a --format "{{.Names}}" | grep "^baxbench-" | xargs -r docker rm -f
  fi
fi

# 3) Show any remaining listeners for the typical ports used by BaxBench
ss -ltnp | egrep ":(5001|1700[0-9]|1543[0-9])\b" || true
'"'"''
done


#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
remote_docker_prune.sh - prune docker state on remote hosts over SSH

USAGE:
  ./scripts/distributed_bench/remote_docker_prune.sh --hosts "host1 host2 host3" [options]

OPTIONS:
  --hosts "<h1 h2 ...>"   Whitespace-separated SSH hostnames (required)
  --containers            Remove ALL containers on each host (default: on)
  --no-containers         Do not remove containers
  --images                Prune images on each host (default: on)
  --no-images             Do not prune images
  --volumes               ALSO remove anonymous volumes (default: off)
  --dry-run               Print actions only; do not execute
  --parallel N            Max concurrent hosts (default: 8)
  --ssh-opts "<opts>"     Extra ssh options (default: none)
  --force                 Do not prompt for confirmation
  -h, --help              Show this help

NOTES:
  - This is destructive. It can delete unrelated containers/images on the target machines.
  - Intended for remote benchmark hosts that are safe to clean.
  - Requires passwordless SSH access or an ssh-agent.

EXAMPLES:
  ./scripts/distributed_bench/remote_docker_prune.sh --hosts "app1 app2 load1" --dry-run
  ./scripts/distributed_bench/remote_docker_prune.sh --hosts "app1 app2 load1" --parallel 4 --force
  ./scripts/distributed_bench/remote_docker_prune.sh --hosts "app1 app2" --no-images --containers --force
EOF
}

HOSTS="r630-02 r630-03 r630-04 r630-05 r630-08"
DO_CONTAINERS=1
DO_IMAGES=0
DO_VOLUMES=1
DRY_RUN=0
PARALLEL=8
SSH_OPTS=""
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts)
      HOSTS="${2:-}"; shift 2;;
    --containers)
      DO_CONTAINERS=1; shift;;
    --no-containers)
      DO_CONTAINERS=0; shift;;
    --images)
      DO_IMAGES=1; shift;;
    --no-images)
      DO_IMAGES=0; shift;;
    --volumes)
      DO_VOLUMES=1; shift;;
    --dry-run)
      DRY_RUN=1; shift;;
    --parallel)
      PARALLEL="${2:-}"; shift 2;;
    --ssh-opts)
      SSH_OPTS="${2:-}"; shift 2;;
    --force)
      FORCE=1; shift;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2;;
  esac
done

if [[ -z "${HOSTS// }" ]]; then
  echo "ERROR: --hosts is required" >&2
  usage
  exit 2
fi

if ! [[ "$PARALLEL" =~ ^[0-9]+$ ]] || [[ "$PARALLEL" -lt 1 ]]; then
  echo "ERROR: --parallel must be a positive integer" >&2
  exit 2
fi

echo "Targets: $HOSTS"
echo "Actions:"
echo "  - remove containers: $DO_CONTAINERS"
echo "  - prune images:      $DO_IMAGES"
echo "  - remove volumes:    $DO_VOLUMES"
echo "  - dry-run:           $DRY_RUN"
echo "  - parallel hosts:    $PARALLEL"
echo

if [[ "$FORCE" -ne 1 && "$DRY_RUN" -ne 1 ]]; then
  read -r -p "Type 'prune' to continue: " confirm
  if [[ "$confirm" != "prune" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

remote_cmd() {
  local host="$1"

  # Build a single remote bash script to minimize SSH round-trips.
  # We intentionally avoid relying on any local repo files.
  local script="set -euo pipefail;"

  script+="echo \"==> \$(hostname)\";"
  script+="if ! command -v docker >/dev/null 2>&1; then echo \"docker not found\" >&2; exit 0; fi;"

  if [[ "$DO_CONTAINERS" -eq 1 ]]; then
    # Remove all containers (running + stopped).
    script+="ids=\$(docker ps -aq || true);"
    script+="if [ -n \"\${ids}\" ]; then docker rm -f \${ids} >/dev/null; fi;"
  fi

  if [[ "$DO_IMAGES" -eq 1 ]]; then
    # Prune images (includes dangling + unused). Use -a to remove unused images, not only dangling.
    script+="docker image prune -af >/dev/null;"
  fi

  if [[ "$DO_VOLUMES" -eq 1 ]]; then
    # Remove anonymous volumes not used by any container.
    script+="docker volume prune -f >/dev/null;"
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run][$host] ssh $SSH_OPTS $host 'bash -lc <script>'"
    return 0
  fi

  # shellcheck disable=SC2086
  ssh $SSH_OPTS "$host" "bash -lc $(printf '%q' "$script")"
}

export -f remote_cmd
export DO_CONTAINERS DO_IMAGES DO_VOLUMES DRY_RUN SSH_OPTS

# NOTE: With `xargs -I`, each *input line* becomes one replacement item.
# So ensure we feed one host per line (not a single space-separated line),
# otherwise SSH will see an invalid hostname containing spaces.
printf '%s\n' $HOSTS | xargs -P "$PARALLEL" -I '{}' bash -lc 'remote_cmd "$@"' _ '{}'

echo
echo "Done."


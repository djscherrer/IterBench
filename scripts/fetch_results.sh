#!/usr/bin/env bash
#
# Run this script on the local machine to stream the remote BaxBench results
# directory into a compressed archive without creating a remote archive first.
#
# Usage:
#   ./scripts/fetch_results.sh USER@HOST [REMOTE_REPO] [LOCAL_OUTPUT_DIR]
#       [REMOTE_RESULTS_DIR]
#
# Example:
#   ./scripts/fetch_results.sh dscherre@node0 \
#     /tmp/dscherre/baxbench \
#     ~/Downloads results_reverified

set -euo pipefail

if [[ $# -lt 1 || $# -gt 4 ]]; then
  echo "Usage: $0 USER@HOST [REMOTE_REPO] [LOCAL_OUTPUT_DIR] [REMOTE_RESULTS_DIR]" >&2
  exit 2
fi

remote_host=$1
remote_repo=${2:-/tmp/dscherre/baxbench}
output_dir=${3:-.}
remote_results_dir=${4:-results_reverified}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$output_dir/baxbench-$(basename "$remote_results_dir")-$timestamp.tar.gz"
partial="$archive.part"

mkdir -p "$output_dir"
trap 'rm -f "$partial"' EXIT

echo "Fetching $remote_host:$remote_repo/$remote_results_dir"
echo "Writing $archive"

ssh -T "$remote_host" bash -s -- "$remote_repo" "$remote_results_dir" >"$partial" <<'REMOTE'
set -euo pipefail

remote_repo=$1
remote_results_dir=$2
results_dir="$remote_repo/$remote_results_dir"

if [[ ! -d "$results_dir" ]]; then
  echo "Results directory does not exist: $results_dir" >&2
  exit 1
fi

# The remote re-verification tree may still be active.  GNU tar returns
# status 1 when a file changes while it is being read; the streamed archive
# is still usable for the completed files, so tolerate that specific status
# but keep real tar/gzip failures fatal.
set +e
tar -C "$remote_repo" -cf - "$remote_results_dir" | gzip -1
pipeline_status=("${PIPESTATUS[@]}")
tar_status=${pipeline_status[0]}
gzip_status=${pipeline_status[1]}
set -e

if [[ $gzip_status -ne 0 || $tar_status -gt 1 ]]; then
  echo "Remote archive failed (tar=$tar_status gzip=$gzip_status)" >&2
  exit 1
fi
if [[ $tar_status -eq 1 ]]; then
  echo "Warning: remote files changed while the archive was being read; continuing with this snapshot." >&2
fi
REMOTE

mv "$partial" "$archive"
trap - EXIT

echo "Done: $archive"
echo "Extract with: tar -xzf \"$archive\""

#!/usr/bin/env bash
#
# Run this script on the local machine to stream the remote BaxBench results
# directory into a compressed archive without creating a remote archive first.
#
# Usage:
#   ./scripts/fetch_results.sh USER@HOST [REMOTE_REPO] [LOCAL_OUTPUT_DIR]
#
# Example:
#   ./scripts/fetch_results.sh dscherre@node0 \
#     /tmp/dscherre/baxbench \
#     ~/Downloads

set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: $0 USER@HOST [REMOTE_REPO] [LOCAL_OUTPUT_DIR]" >&2
  exit 2
fi

remote_host=$1
remote_repo=${2:-/tmp/dscherre/baxbench}
output_dir=${3:-.}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$output_dir/baxbench-results-$timestamp.tar.gz"
partial="$archive.part"

mkdir -p "$output_dir"
trap 'rm -f "$partial"' EXIT

echo "Fetching $remote_host:$remote_repo/results"
echo "Writing $archive"

ssh -T "$remote_host" bash -s -- "$remote_repo" >"$partial" <<'REMOTE'
set -euo pipefail

remote_repo=$1
results_dir="$remote_repo/results"

if [[ ! -d "$results_dir" ]]; then
  echo "Results directory does not exist: $results_dir" >&2
  exit 1
fi

tar -C "$remote_repo" -cf - results | gzip -1
REMOTE

mv "$partial" "$archive"
trap - EXIT

echo "Done: $archive"
echo "Extract with: tar -xzf \"$archive\""

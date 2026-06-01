#!/bin/bash
# BaxBench - Copy an existing iteration into a fresh "what-if" experiment slug.
#
# Goal: hand-tweak the deployment spec (or the application code) of an iteration
# you already ran, then re-run JUST the deploy probe + Locust bench against it,
# without going through the LLM-driven refinement loop.
#
# Usage:
#   ./scripts/k8s_replay_iteration.sh \
#       --src /tmp/dscherre/baxbench/results/<...>/sample3/k8s-experiments/expB/iterations/iteration-000-baseline \
#       --to manualA
#
#   # optional: rename the iteration in the destination
#   ./scripts/k8s_replay_iteration.sh --src <path> --to manualA --as iteration-000-baseline
#
# What it does:
#   1. Copies the source iteration into <sample>/k8s-experiments/<to>/iterations/<as>/.
#   2. Wipes 04-deploy/{probe,bench}.json and 05-bench/* so the deploy-only run
#      actually re-deploys (the probe-reuse check + has_k8s_perf_run_for_iteration
#      both treat those as the "already done" markers).
#   3. Prints the exact bench_k8s.sh invocation to replay it.
#
# After the copy, edit either:
#   - <dest>/03-spec/spec.yaml             (replicas, CPU/mem, max_connections, …)
#   - <dest>/02-code/code/app.js  (or .py) (iteration-local code snapshot; picked
#                                          up by latest_code_dir over the
#                                          sample-level code/ baseline)
#
# Then run the printed command. Each "what-if" gets its own k8s-experiments/<to>/
# folder, so you can keep several side by side.

set -euo pipefail

SRC=""
TO=""
AS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2;;
    --to) TO="$2"; shift 2;;
    --as) AS="$2"; shift 2;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$SRC" ] || [ -z "$TO" ]; then
  echo "Usage: $0 --src <path-to-iteration> --to <experiment-slug> [--as <iteration-id>]" >&2
  exit 2
fi

if [ ! -d "$SRC" ]; then
  echo "Source iteration folder does not exist: $SRC" >&2
  exit 1
fi

# Validate that the source path lives under .../k8s-experiments/<slug>/iterations/<id>
case "$SRC" in
  */k8s-experiments/*/iterations/*) ;;
  *)
    echo "Source path does not look like .../k8s-experiments/<slug>/iterations/<id>:" >&2
    echo "  $SRC" >&2
    exit 1
    ;;
esac

SRC_ITER_ID="$(basename "$SRC")"
DEST_ITER_ID="${AS:-$SRC_ITER_ID}"

# Derive <sample>/k8s-experiments/ root from the source path.
ITER_ROOT="$(dirname "$SRC")"                              # .../iterations
EXP_DIR="$(dirname "$ITER_ROOT")"                          # .../k8s-experiments/<src-slug>
EXP_ROOT="$(dirname "$EXP_DIR")"                           # .../k8s-experiments
SRC_SLUG="$(basename "$EXP_DIR")"

DEST_EXP="$EXP_ROOT/$TO"
DEST="$DEST_EXP/iterations/$DEST_ITER_ID"

if [ "$SRC_SLUG" = "$TO" ] && [ "$SRC_ITER_ID" = "$DEST_ITER_ID" ]; then
  echo "Refusing to overwrite the source iteration in place." >&2
  echo "  src:  $SRC" >&2
  echo "  dest: $DEST" >&2
  exit 1
fi

if [ -e "$DEST" ]; then
  echo "Destination already exists: $DEST" >&2
  echo "Pick a different --to or --as, or remove it manually." >&2
  exit 1
fi

mkdir -p "$DEST_EXP/iterations"
cp -a "$SRC" "$DEST"

# Wipe deploy + bench markers so the deploy-only run actually re-deploys + re-benchmarks.
for f in "$DEST/04-deploy/probe.json" "$DEST/04-deploy/bench.json"; do
  [ -e "$f" ] && rm -f "$f"
done
if [ -d "$DEST/05-bench" ]; then
  rm -rf "$DEST/05-bench"
fi
# Also drop the per-iteration outcome log so a fresh one is written.
[ -e "$DEST/iteration.log" ] && rm -f "$DEST/iteration.log"

echo "Copied $SRC"
echo "    -> $DEST"
echo
echo "Next steps:"
echo "  1. Edit the spec (most common case):"
echo "       \$EDITOR $DEST/03-spec/spec.yaml"
echo "  2. (Optional) Code tweak — must live under the iteration snapshot, not sample3/code/:"
echo "       mkdir -p $DEST/02-code/code"
echo "       cp my-app.js $DEST/02-code/code/app.js"
echo "     (A rebuild runs only when 02-code/code/ differs from the sample baseline.)"
echo "  3. Replay with the deploy-only path:"
echo "       K8S_EXPERIMENT=$TO K8S_ITERATION=$DEST_ITER_ID K8S_SPEC_GEN=false ./scripts/bench_k8s.sh"
echo
echo "  Outputs land in $DEST_EXP/  (separate from $SRC_SLUG/)."

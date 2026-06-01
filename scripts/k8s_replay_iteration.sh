#!/bin/bash
# BaxBench - Replay one iteration's deploy + bench (no LLM, no refinement).
#
# Point this at an iteration folder; the script parses the rest of the
# parameters (scenario, env, sample, temperature, ...) from the path, optionally
# copies the iteration into a fresh experiment slug, wipes the deploy + bench
# markers so the run isn't skipped, and then invokes the same code path
# bench_k8s.sh uses for deploy-only runs (`K8S_SPEC_GEN=false`).
#
# Image policy (handled inside ensure_docker_image):
#   - If <iter>/02-code/code/ exists and differs from sampleN/code/, the image
#     is rebuilt from that snapshot (so hand-edited app.js takes effect).
#   - Otherwise the cached functional-test image is reused (spec-only replay
#     stays fast).
# Spec policy: manifests are re-rendered from <iter>/03-spec/spec.yaml on every
# run, so spec edits always take effect.
#
# Typical workflow:
#   1. Copy / edit an iteration manually:
#        cp -a .../k8s-experiments/expb/iterations/iteration-018-spec \
#              .../k8s-experiments/expb/manual_experiments/iteration-018-spec
#        $EDITOR .../manual_experiments/iteration-018-spec/03-spec/spec.yaml
#        # optional: mkdir -p .../manual_experiments/.../02-code/code && cp app.js there
#   2. Replay (auto-derives everything else from the path):
#        ./scripts/k8s_replay_iteration.sh \
#            --iter .../manual_experiments/iteration-018-spec \
#            --to   replay-018-tweak
#
# Flags:
#   --iter <path>        Required. Path to the iteration directory to replay.
#   --to <slug>          Destination experiment slug. Required when --iter is
#                        outside k8s-experiments/<slug>/iterations/; optional
#                        otherwise (defaults to running in place under the
#                        source slug).
#   --model <m>          Override MODEL.   --provider <p>  Override PROVIDER.
#   --cluster <c>        Override CLUSTER. --load-profile <lp> Override profile.
#   --keep-bench         Do NOT wipe 04-deploy/ and 05-bench/ before running.
#                        Lets you point at an iteration that has never been
#                        benched yet without losing existing artifacts.

set -euo pipefail

# === EDIT THESE (overridable via --flags) ===========================
MODEL="deepseek/deepseek-v3.2"        # provider/name (slash form)
PROVIDER="openrouter"
CLUSTER="baxbench-emulab"
LOAD_PROFILE="k8s-adaptive-v2"
TIMEOUT="600"
WAIT_TIMEOUT="120"
PORT="5001"
LLM_MAX_COST="10"
# ====================================================================

ITER=""
TO=""
KEEP_BENCH="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --iter)         ITER="$2"; shift 2;;
    --to)           TO="$2"; shift 2;;
    --model)        MODEL="$2"; shift 2;;
    --provider)     PROVIDER="$2"; shift 2;;
    --cluster)      CLUSTER="$2"; shift 2;;
    --load-profile) LOAD_PROFILE="$2"; shift 2;;
    --keep-bench)   KEEP_BENCH="true"; shift;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0;;
    *)              echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

if [ -z "$ITER" ]; then
  echo "Usage: $0 --iter <path-to-iteration> [--to <slug>] [--model <provider/name>]" >&2
  exit 2
fi
if [ ! -d "$ITER" ]; then
  echo "Iteration path is not a directory: $ITER" >&2
  exit 1
fi
ITER="$(cd "$ITER" && pwd)"

# --- Parse the path ---------------------------------------------------------
# Canonical: results/<model-esc>/<scenario>/<env>/<temp>-<spec>-<safety>/sample<N>/k8s-experiments/<slug>/iterations/<iter-id>
# Non-canonical (e.g. .../k8s-experiments/<src>/manual_experiments/<iter-id>)
# is tolerated; --to is then required.

ITER_ID="$(basename "$ITER")"
P="$(dirname "$ITER")"
SRC_SLUG=""
NEEDS_COPY="false"
K8S_EXP_ROOT=""

case "$P" in
  */k8s-experiments/*/iterations)
    SRC_SLUG="$(basename "$(dirname "$P")")"
    K8S_EXP_ROOT="$(dirname "$(dirname "$P")")"
    ;;
  */k8s-experiments/*)
    NEEDS_COPY="true"
    Q="$P"
    while [ "$(basename "$Q")" != "k8s-experiments" ] && [ "$Q" != "/" ]; do
      Q="$(dirname "$Q")"
    done
    if [ "$Q" = "/" ]; then
      echo "Could not locate a k8s-experiments/ ancestor for $ITER" >&2
      exit 1
    fi
    K8S_EXP_ROOT="$Q"
    ;;
  *)
    echo "Path does not live under any k8s-experiments/ directory: $ITER" >&2
    exit 1
    ;;
esac

SAMPLE_DIR="$(dirname "$K8S_EXP_ROOT")"
SAMPLE_BASENAME="$(basename "$SAMPLE_DIR")"
SAMPLE_NUM="${SAMPLE_BASENAME#sample}"
if ! [[ "$SAMPLE_NUM" =~ ^[0-9]+$ ]]; then
  echo "Could not parse sample number from $SAMPLE_DIR (basename=$SAMPLE_BASENAME)" >&2
  exit 1
fi

CONFIG_DIR="$(dirname "$SAMPLE_DIR")"
CONFIG_BASENAME="$(basename "$CONFIG_DIR")"
if [[ "$CONFIG_BASENAME" =~ ^temp([0-9.]+)-([a-z_]+)-(.+)$ ]]; then
  TEMP="${BASH_REMATCH[1]}"
  SPEC_TYPE="${BASH_REMATCH[2]}"
  SAFETY="${BASH_REMATCH[3]}"
else
  echo "Could not parse temperature/spec/safety from '$CONFIG_BASENAME'" >&2
  echo "Expected something like 'temp0.2-openapi-high_performance'." >&2
  exit 1
fi

ENV_ID="$(basename "$(dirname "$CONFIG_DIR")")"
SCENARIO_ID="$(basename "$(dirname "$(dirname "$CONFIG_DIR")")")"

# --- Destination ------------------------------------------------------------
DEST_SLUG="${TO:-$SRC_SLUG}"
if [ -z "$DEST_SLUG" ]; then
  echo "Iteration sits outside an iterations/ folder; pass --to <slug>." >&2
  exit 1
fi
DEST_ITER_DIR="$K8S_EXP_ROOT/$DEST_SLUG/iterations/$ITER_ID"

DO_COPY="false"
if [ "$NEEDS_COPY" = "true" ]; then DO_COPY="true"; fi
if [ -n "$TO" ] && [ "$TO" != "$SRC_SLUG" ]; then DO_COPY="true"; fi

if [ "$DO_COPY" = "true" ]; then
  if [ -e "$DEST_ITER_DIR" ]; then
    echo "Destination already exists: $DEST_ITER_DIR" >&2
    echo "Remove it manually or pick a different --to slug." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$DEST_ITER_DIR")"
  cp -a "$ITER" "$DEST_ITER_DIR"
  echo "Copied: $ITER"
  echo "    -> $DEST_ITER_DIR"
fi

# --- Wipe stale deploy/bench markers ---------------------------------------
if [ "$KEEP_BENCH" != "true" ]; then
  rm -f "$DEST_ITER_DIR/04-deploy/probe.json" "$DEST_ITER_DIR/04-deploy/bench.json"
  rm -rf "$DEST_ITER_DIR/05-bench"
  rm -f "$DEST_ITER_DIR/iteration.log"
  echo "Cleared 04-deploy/{probe,bench}.json, 05-bench/, iteration.log under $DEST_ITER_DIR"
fi

# --- Resolve KUBECONFIG from the cluster profile (mirrors bench_k8s.sh) ----
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
_kc=""
if [ -n "$CLUSTER" ]; then
  _kc=$(cd "$ROOT" && pipenv run python -c "
from k8s_bench.cluster import resolve_cluster_profile
import os
p = resolve_cluster_profile('$CLUSTER')
print(os.path.expanduser(p.kubeconfig_path) if p.kubeconfig_path else '')
" 2>/dev/null | tail -n 1) || true
  if [ -n "$_kc" ]; then
    export KUBECONFIG="$_kc"
  fi
fi
echo "kubectl context: $(kubectl config current-context 2>/dev/null || echo '(not configured)')"

# --- Invoke main.py (deploy-only path) -------------------------------------
ARGS=(
  --mode k8s-bench
  --models "$MODEL"
  --provider "$PROVIDER"
  --only_samples "$SAMPLE_NUM"
  --envs "$ENV_ID"
  --scenarios "$SCENARIO_ID"
  --temperature "$TEMP"
  --safety_prompt "$SAFETY"
  --k8s-cluster "$CLUSTER"
  --k8s-iteration "$ITER_ID"
  --k8s-experiment "$DEST_SLUG"
  --k8s-iterations 0
  --k8s-wait-timeout "$WAIT_TIMEOUT"
  --k8s-refinement off
  --no-k8s-spec-gen
  --force
  --timeout "$TIMEOUT"
  --port "$PORT"
)

EXTRA_ENV=(
  "BAXBENCH_LOAD_PROFILE=$LOAD_PROFILE"
  "BAXBENCH_K8S_CLUSTER=$CLUSTER"
  "BAXBENCH_K8S_EXPERIMENT=$DEST_SLUG"
  "BAXBENCH_K8S_ITERATION=$ITER_ID"
  "BAXBENCH_LLM_MAX_COST=$LLM_MAX_COST"
)
if [ -n "${KUBECONFIG:-}" ]; then
  EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG")
fi

cat <<EOF

=== Replaying iteration ===
  iter dir   : $DEST_ITER_DIR
  slug       : $DEST_SLUG
  model      : $MODEL  (provider=$PROVIDER)
  scenario   : $SCENARIO_ID
  env        : $ENV_ID
  sample     : $SAMPLE_NUM
  config     : temp=$TEMP spec=$SPEC_TYPE safety=$SAFETY
  cluster    : $CLUSTER   load_profile=$LOAD_PROFILE

EOF

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

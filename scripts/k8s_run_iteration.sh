#!/bin/bash
# BaxBench - Run deploy + Locust bench for one iteration folder, in place.
#
# Point --iter at any iteration directory under sampleN/ (wherever you put it).
# Edit 03-spec/spec.yaml and/or 02-code/code/ there, run this script; results
# land in that same folder (04-deploy/, 05-bench/, iteration.log).
#
# Example:
#   ./scripts/k8s_run_iteration.sh \
#       --iter results/.../sample3/k8s-experiments/expb/manual_experiments/iteration-018-spec
#
# Flags:
#   --iter <path>        Required. Iteration folder (relative or absolute).
#   --model <m>          Override MODEL.       --provider <p>  Override PROVIDER.
#   --cluster <c>        Override CLUSTER.     --load-profile <lp> Override profile.
#   --keep-bench         Skip wiping 04-deploy/ + 05-bench/ before the run.

set -euo pipefail

# === EDIT THESE (overridable via --flags) ===========================
MODEL="deepseek/deepseek-v4-pro"        # provider/name (slash form)
PROVIDER="openrouter"
CLUSTER="baxbench-emulab"
LOAD_PROFILE="k8s-goodput-plateau"
TIMEOUT="300"
WAIT_TIMEOUT="180"
PORT="5001"
LLM_MAX_COST="10"
# ====================================================================

ITER="results/deepseek-deepseek-v4-pro/LexiTally_WordCountDatasets/Python-Flask/temp0.2-openapi-high_performance/sample0/k8s-experiments/12-6-bench-lp-improved2/manual/iteration-010-code-plateau-5"
KEEP_BENCH="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --iter)         ITER="$2"; shift 2;;
    --model)        MODEL="$2"; shift 2;;
    --provider)     PROVIDER="$2"; shift 2;;
    --cluster)      CLUSTER="$2"; shift 2;;
    --load-profile) LOAD_PROFILE="$2"; shift 2;;
    --keep-bench)   KEEP_BENCH="true"; shift;;
    -h|--help)      sed -n '2,20p' "$0"; exit 0;;
    *)              echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

if [ -z "$ITER" ]; then
  echo "Usage: $0 --iter <path-to-iteration-folder>" >&2
  exit 2
fi
if [ ! -d "$ITER" ]; then
  echo "Iteration path is not a directory: $ITER" >&2
  exit 1
fi
ITER="$(cd "$ITER" && pwd)"
ITER_ID="$(basename "$ITER")"

# Find sampleN/ and parse BaxBench task coordinates from the path.
SAMPLE_DIR=""
CUR="$ITER"
while [ "$CUR" != "/" ] && [ -n "$CUR" ]; do
  CUR="$(dirname "$CUR")"
  BN="$(basename "$CUR")"
  if [[ "$BN" =~ ^sample[0-9]+$ ]]; then
    SAMPLE_DIR="$CUR"; break
  fi
done
if [ -z "$SAMPLE_DIR" ]; then
  echo "Could not find a 'sampleN/' ancestor of $ITER." >&2
  exit 1
fi
SAMPLE_NUM="${BN#sample}"

CONFIG_DIR="$(dirname "$SAMPLE_DIR")"
CONFIG_BN="$(basename "$CONFIG_DIR")"
if [[ "$CONFIG_BN" =~ ^temp([0-9.]+)-([a-z_]+)-(.+)$ ]]; then
  TEMP="${BASH_REMATCH[1]}"
  SPEC_TYPE="${BASH_REMATCH[2]}"
  SAFETY="${BASH_REMATCH[3]}"
else
  echo "Could not parse temp/spec/safety from '$CONFIG_BN'." >&2
  exit 1
fi
ENV_ID="$(basename "$(dirname "$CONFIG_DIR")")"
SCENARIO_ID="$(basename "$(dirname "$(dirname "$CONFIG_DIR")")")"

if [ "$KEEP_BENCH" != "true" ]; then
  rm -f "$ITER/04-deploy/probe.json" "$ITER/04-deploy/bench.json"
  rm -rf "$ITER/05-bench"
  rm -f "$ITER/iteration.log"
  echo "Cleared stale deploy/bench artifacts under $ITER"
fi

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
  --k8s-iteration-path "$ITER"
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
  "BAXBENCH_LLM_MAX_COST=$LLM_MAX_COST"
)
if [ -n "${KUBECONFIG:-}" ]; then
  EXTRA_ENV+=("KUBECONFIG=$KUBECONFIG")
fi

cat <<EOF

=== Running iteration (in place) ===
  iter dir   : $ITER
  scenario   : $SCENARIO_ID
  env        : $ENV_ID
  sample     : $SAMPLE_NUM
  cluster    : $CLUSTER   load_profile=$LOAD_PROFILE

EOF

(cd "$ROOT" && env "${EXTRA_ENV[@]}" pipenv run python src/main.py "${ARGS[@]}")

echo
echo "Done. Results under: $ITER"

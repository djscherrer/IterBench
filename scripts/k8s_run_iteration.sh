#!/bin/bash
# BaxBench - Run deploy + Locust bench for one iteration folder, in place.
#
# Point --iter at any iteration directory under sampleN/ (wherever you put it).
# Edit 03-spec/spec.yaml and/or 02-code/code/ there, run this script; results
# land in that same folder (04-deploy/, 05-bench/, iteration.log).
#
# Thin wrapper around the SAME stages the full experiment runs. It calls
# src/main.py --deploy-only, which dispatches to execute_deploy_only_iteration
# (orchestration/deploy_only.py) and mirrors the experiment's tail:
#   * 04-deploy  patches the registry image + port + labels onto 03-spec/spec.yaml
#                in memory, applies manifests, writes 04-deploy/probe.json.
#   * 05-bench   runs distributed Locust against the probe target.
#   * 06-outcome writes iteration_feedback.json + the experiment_summary.md block.
# No LLM stages run: 02-code/code/ and 03-spec/spec.yaml must already exist.
#
# Task coordinates (model, scenario, env, sample, temperature, …) are derived
# from the results path — you only need --iter plus cluster/load knobs.
#
# Example:
#   ./scripts/k8s_run_iteration.sh \
#       --iter results/.../sample3/k8s-experiments/expb/manual/iteration-018-spec
#
# Flags:
#   --iter <path>        Required. Iteration folder (relative or absolute).
#   --cluster <c>        Override CLUSTER.     --load-profile <lp> Override profile.
#   --keep-bench         Skip wiping 04-deploy/ + 05-bench/ before the run.

set -euo pipefail

# === EDIT THESE (overridable via --flags) ===========================
CLUSTER="baxbench-emulab"
LOAD_PROFILE="k8s-explore-refine"
TIMEOUT="600"
WAIT_TIMEOUT="600"
PORT="5001"
# ====================================================================

ITER="results/openai-gpt-5.5-2026-04-23/Petstore/Go-net-http/temp0.2-openapi-high_performance/sample0/k8s-experiments/old_results/manual/iteration-002-code"
KEEP_BENCH="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --iter)         ITER="$2"; shift 2;;
    --cluster)      CLUSTER="$2"; shift 2;;
    --load-profile) LOAD_PROFILE="$2"; shift 2;;
    --keep-bench)   KEEP_BENCH="true"; shift;;
    -h|--help)      sed -n '2,28p' "$0"; exit 0;;
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
MODEL_ESC="$(basename "$(dirname "$(dirname "$(dirname "$CONFIG_DIR")")")")"
# Results paths sanitize env IDs to be filesystem-friendly. BaxBench env IDs are
# `${language}-${framework}` where framework may contain `/` (e.g. `net/http`).
# Reverse the known sanitization for those envs so --envs filtering works.
if [[ "$ENV_ID" == "Go-net-http" ]]; then
  ENV_ID="Go-net/http"
fi
# results/<provider>-<model>/... → provider/model for --models
MODEL="${MODEL_ESC/-//}"
RESULTS_DIR="$(dirname "$(dirname "$(dirname "$(dirname "$(dirname "$SAMPLE_DIR")")")")")"

if [ "$KEEP_BENCH" != "true" ]; then
  rm -rf "$ITER/04-deploy"
  rm -rf "$ITER/05-bench"
  rm -f "$ITER/iteration.log"
  echo "Cleared stale 04-deploy/05-bench artifacts under $ITER"
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

# Prefer the project venv; system pipenv (e.g. 11.9.0 on Ubuntu) often breaks on Python 3.10+.
baxbench_python() {
  if [ -x "$ROOT/.venv/bin/python" ]; then
    "$ROOT/.venv/bin/python" "$@"
  elif command -v pipenv >/dev/null 2>&1 && pipenv --version >/dev/null 2>&1; then
    pipenv run python "$@"
  else
    python3 "$@"
  fi
}

_kc=""
if [ -n "$CLUSTER" ]; then
  _kc=$(cd "$ROOT" && baxbench_python -c "
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
  --only_samples "$SAMPLE_NUM"
  --envs "$ENV_ID"
  --scenarios "$SCENARIO_ID"
  --temperature "$TEMP"
  --safety_prompt "$SAFETY"
  --spec_type "$SPEC_TYPE"
  --k8s-cluster "$CLUSTER"
  --k8s-iteration-path "$ITER"
  --k8s-iterations 0
  --k8s-wait-timeout "$WAIT_TIMEOUT"
  --load-profile "$LOAD_PROFILE"
  --deploy-only
  --force
  --timeout "$TIMEOUT"
  --port "$PORT"
  --results_dir "$RESULTS_DIR"
)

EXTRA_ENV=()

cat <<EOF

=== Running iteration (in place) ===
  iter dir   : $ITER
  model      : $MODEL
  scenario   : $SCENARIO_ID
  env        : $ENV_ID
  sample     : $SAMPLE_NUM
  cluster    : $CLUSTER   load_profile=$LOAD_PROFILE

EOF

(
  cd "$ROOT"
  if [ -n "${KUBECONFIG:-}" ]; then
    export KUBECONFIG
  fi
  baxbench_python src/main.py "${ARGS[@]}"
)

echo
echo "Done. Results under: $ITER"

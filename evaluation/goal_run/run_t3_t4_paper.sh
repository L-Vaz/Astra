#!/usr/bin/env bash
# Run tiers 3 (cylinder gmsh) and 4 (tandem wing external mesh) with the
# verbatim paper prompts (Appendix B.3 and B.2). Adapted from run_all_tiers.sh.
# Usage: RUN_TAG=paper_run1 bash run_t3_t4_paper.sh
set -uo pipefail

WORKSPACE_EVAL="/opt/Foam-Eval"
FOAM_AGENT="/opt/Foam-Agent"
PROMPT_DIR="$WORKSPACE_EVAL/goal_run/prompts"
RUN_TAG="${RUN_TAG:-paper_run1}"
LOG_DIR="$WORKSPACE_EVAL/goal_run/logs_$RUN_TAG"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-$HOME/foamagent-goal/${RUN_TAG}_$RUN_ID}"

mkdir -p "$LOG_DIR" "$RUN_ROOT"
printf '%s\n' "$RUN_ROOT" > "$LOG_DIR/run_root.txt"

set +u
set -a
if [ -f "$HOME/.foam.env" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.foam.env"
fi
set +a
source /opt/openfoam10/etc/bashrc
set -u
export WM_PROJECT_DIR="${WM_PROJECT_DIR:-/opt/openfoam10}"
export PATH="/opt/conda/envs/FoamAgent/bin:/opt/openfoam10/platforms/linux64GccDPInt32Opt/bin:/opt/openfoam10/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYVISTA_OFF_SCREEN="${PYVISTA_OFF_SCREEN:-true}"
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export FOAMAGENT_MODEL_PROVIDER="${FOAMAGENT_MODEL_PROVIDER:-anthropic}"
export FOAMAGENT_MODEL_VERSION="${FOAMAGENT_MODEL_VERSION:-claude-sonnet-4-6}"

PY=/opt/conda/envs/FoamAgent/bin/python

run_tier() {
  local tier="$1"
  local label="$2"
  local prompt="$3"
  local custom_mesh="${4:-}"
  local out_dir="$RUN_ROOT/$tier"
  local log_file="$LOG_DIR/$tier.log"
  local time_file="$LOG_DIR/$tier.time"
  local status_file="$LOG_DIR/$tier.status"
  local started finished rc

  started="$(date -Is)"
  echo "===== $tier START $started :: $label ====="

  cd "$FOAM_AGENT"
  set +e
  if [ -n "$custom_mesh" ]; then
    /usr/bin/time -v -o "$time_file" "$PY" foambench_main.py \
      --output "$out_dir" \
      --prompt_path "$prompt" \
      --custom_mesh_path "$custom_mesh" \
      > "$log_file" 2>&1
  else
    /usr/bin/time -v -o "$time_file" "$PY" foambench_main.py \
      --output "$out_dir" \
      --prompt_path "$prompt" \
      > "$log_file" 2>&1
  fi
  rc=$?
  set -e

  finished="$(date -Is)"
  {
    echo "tier=$tier"
    echo "label=$label"
    echo "started=$started"
    echo "finished=$finished"
    echo "exit_code=$rc"
    echo "output_dir=$out_dir"
    echo "log_file=$log_file"
  } > "$status_file"
  echo "===== $tier END $finished :: exit_code=$rc ====="
  set +e
}

run_tier "tier3" "cylinder / gmsh / pisoFoam (paper B.3)" "$PROMPT_DIR/tier3_cylinder_gmsh_paper.txt"
run_tier "tier4" "tandem wing / external msh / simpleFoam SA (paper B.2)" "$PROMPT_DIR/tier4_tandem_wing_paper.txt" "$FOAM_AGENT/tandem_wing.msh"

echo "done run_root=$RUN_ROOT" > "$LOG_DIR/DONE"
echo "all_finished run_root=$RUN_ROOT logs=$LOG_DIR"

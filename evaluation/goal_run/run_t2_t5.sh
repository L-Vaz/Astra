#!/usr/bin/env bash
# Tier 2 (pitzDaily RAS) + T5 mini-benchmark: 10 cases, one per paper physics
# category, all standard-mesh (blockMesh). Usage: RUN_TAG=t2t5_run1 bash run_t2_t5.sh
set -uo pipefail

WORKSPACE_EVAL="/opt/Foam-Eval"
FOAM_AGENT="/opt/Foam-Agent"
PROMPT_DIR="$WORKSPACE_EVAL/goal_run/prompts"
RUN_TAG="${RUN_TAG:-t2t5_run1}"
LOG_DIR="$WORKSPACE_EVAL/goal_run/logs_$RUN_TAG"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-$HOME/foamagent-goal/${RUN_TAG}_$RUN_ID}"

mkdir -p "$LOG_DIR" "$RUN_ROOT"
printf '%s\n' "$RUN_ROOT" > "$LOG_DIR/run_root.txt"

set +u
set -a
[ -f "$HOME/.foam.env" ] && source "$HOME/.foam.env"
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

run_case() {
  local name="$1"
  local prompt="$2"
  local out_dir="$RUN_ROOT/$name"
  local log_file="$LOG_DIR/$name.log"
  local status_file="$LOG_DIR/$name.status"
  local started finished rc

  started="$(date -Is)"
  echo "===== $name START $started ====="
  cd "$FOAM_AGENT"
  set +e
  /usr/bin/time -v -o "$LOG_DIR/$name.time" "$PY" foambench_main.py \
    --output "$out_dir" \
    --prompt_path "$prompt" \
    > "$log_file" 2>&1
  rc=$?
  set -e
  finished="$(date -Is)"
  {
    echo "case=$name"
    echo "started=$started"
    echo "finished=$finished"
    echo "exit_code=$rc"
    echo "output_dir=$out_dir"
  } > "$status_file"
  echo "===== $name END $finished :: exit_code=$rc ====="
  set +e
}

run_case "tier2"  "$PROMPT_DIR/tier2_pitzdaily_ras.txt"
run_case "t5_01"  "$PROMPT_DIR/t5_01_laminar_channel.txt"
run_case "t5_02"  "$PROMPT_DIR/t5_02_square_obstacle.txt"
run_case "t5_03"  "$PROMPT_DIR/t5_03_turbulent_flatplate.txt"
run_case "t5_04"  "$PROMPT_DIR/t5_04_turbulent_duct.txt"
run_case "t5_05"  "$PROMPT_DIR/t5_05_heat_conduction.txt"
run_case "t5_06"  "$PROMPT_DIR/t5_06_buoyant_cavity.txt"
run_case "t5_07"  "$PROMPT_DIR/t5_07_shallow_water.txt"
run_case "t5_08"  "$PROMPT_DIR/t5_08_dambreak.txt"
run_case "t5_09"  "$PROMPT_DIR/t5_09_shocktube.txt"
run_case "t5_10"  "$PROMPT_DIR/t5_10_counterflow_flame.txt"

echo "done run_root=$RUN_ROOT" > "$LOG_DIR/DONE"
echo "all_finished run_root=$RUN_ROOT logs=$LOG_DIR"

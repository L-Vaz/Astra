#!/usr/bin/env bash
set -uo pipefail

WORKSPACE_EVAL="/opt/Foam-Eval"
FOAM_AGENT="/opt/Foam-Agent"
PROMPT_DIR="$WORKSPACE_EVAL/goal_run/prompts"
LOG_DIR="$WORKSPACE_EVAL/goal_run/logs"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-$HOME/foamagent-goal/$RUN_ID}"

mkdir -p "$LOG_DIR" "$RUN_ROOT"
printf '%s\n' "$RUN_ROOT" > "$WORKSPACE_EVAL/goal_run/run_root.txt"

set +u
set -a
if [ -f "$HOME/.foam.env" ]; then
  # Load user model/API settings if present. The script sets runtime paths below.
  # shellcheck disable=SC1090
  source "$HOME/.foam.env"
fi
set +a
set -u

set +u
source /opt/openfoam10/etc/bashrc
set -u
export WM_PROJECT_DIR="${WM_PROJECT_DIR:-/opt/openfoam10}"
export PATH="/opt/conda/envs/FoamAgent/bin:/opt/openfoam10/platforms/linux64GccDPInt32Opt/bin:/opt/openfoam10/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYVISTA_OFF_SCREEN="${PYVISTA_OFF_SCREEN:-true}"
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"

export FOAMAGENT_MODEL_PROVIDER="${FOAMAGENT_MODEL_PROVIDER:-openai-codex}"
case "$FOAMAGENT_MODEL_PROVIDER" in
  anthropic)
    export FOAMAGENT_MODEL_VERSION="${FOAMAGENT_MODEL_VERSION:-claude-3-5-sonnet-latest}"
    ;;
  openai-codex|codex|chatgpt-oauth)
    export FOAMAGENT_MODEL_VERSION="${FOAMAGENT_MODEL_VERSION:-gpt-5.5}"
    ;;
  openai)
    export FOAMAGENT_MODEL_VERSION="${FOAMAGENT_MODEL_VERSION:-gpt-5-mini}"
    ;;
  *)
    export FOAMAGENT_MODEL_VERSION="${FOAMAGENT_MODEL_VERSION:-gpt-5.5}"
    ;;
esac

if [[ "$FOAMAGENT_MODEL_PROVIDER" == "openai-codex" || "$FOAMAGENT_MODEL_PROVIDER" == "codex" || "$FOAMAGENT_MODEL_PROVIDER" == "chatgpt-oauth" ]]; then
  PY_RUNNER=(/usr/bin/python3 "$WORKSPACE_EVAL/scripts/run_with_codex_auth.py" /opt/conda/envs/FoamAgent/bin/python)
else
  PY_RUNNER=(/opt/conda/envs/FoamAgent/bin/python)
fi

probe() {
  echo "run_id=$RUN_ID"
  echo "run_root=$RUN_ROOT"
  echo "foam_agent=$FOAM_AGENT"
  echo "workspace_eval=$WORKSPACE_EVAL"
  echo "WM_PROJECT_DIR=$WM_PROJECT_DIR"
  echo "python=$(/opt/conda/envs/FoamAgent/bin/python --version)"
  echo "foamVersion=$(foamVersion 2>/dev/null || true)"
  echo "blockMesh=$(command -v blockMesh || true)"
  echo "gmshToFoam=$(command -v gmshToFoam || true)"
  echo "model_provider=$FOAMAGENT_MODEL_PROVIDER"
  echo "model_version=$FOAMAGENT_MODEL_VERSION"
  if [[ "$FOAMAGENT_MODEL_PROVIDER" == "openai-codex" || "$FOAMAGENT_MODEL_PROVIDER" == "codex" || "$FOAMAGENT_MODEL_PROVIDER" == "chatgpt-oauth" ]]; then
    if [ -f /mnt/c/Users/asus/.codex/auth.json ]; then
      echo "codex_auth=present"
    else
      echo "codex_auth=missing"
    fi
  fi
  cd "$FOAM_AGENT"
  /opt/conda/envs/FoamAgent/bin/python - <<'PY'
import importlib.util
mods = ["langchain", "langgraph", "pyvista", "gmsh", "faiss", "sentence_transformers", "openai"]
for name in mods:
    print(f"import_{name}={bool(importlib.util.find_spec(name))}")
PY
}

run_tier() {
  local tier="$1"
  local label="$2"
  local prompt="$3"
  local custom_mesh="${4:-}"
  local out_dir="$RUN_ROOT/$tier"
  local log_file="$LOG_DIR/$tier.log"
  local time_file="$LOG_DIR/$tier.time"
  local status_file="$LOG_DIR/$tier.status"
  local started
  local finished
  local rc

  started="$(date -Is)"
  echo "===== $tier START $started :: $label ====="
  echo "output_dir=$out_dir"
  echo "prompt=$prompt"
  if [ -n "$custom_mesh" ]; then
    echo "custom_mesh=$custom_mesh"
  fi

  cd "$FOAM_AGENT"
  set +e
  if [ -n "$custom_mesh" ]; then
    /usr/bin/time -v -o "$time_file" "${PY_RUNNER[@]}" foambench_main.py \
      --output "$out_dir" \
      --prompt_path "$prompt" \
      --custom_mesh_path "$custom_mesh" \
      > "$log_file" 2>&1
  else
    /usr/bin/time -v -o "$time_file" "${PY_RUNNER[@]}" foambench_main.py \
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
    echo "time_file=$time_file"
  } > "$status_file"

  echo "===== $tier END $finished :: exit_code=$rc ====="
  echo "----- tail $log_file -----"
  tail -40 "$log_file" || true
  echo "----- end tail $tier -----"
}

if [ "${1:-}" = "--probe" ]; then
  probe
  exit 0
fi

probe | tee "$LOG_DIR/probe.log"

run_tier "tier1" "lid-driven cavity / blockMesh / icoFoam" "$PROMPT_DIR/tier1_lid_driven_cavity.txt"
run_tier "tier2" "pitzDaily / blockMesh / simpleFoam RAS" "$PROMPT_DIR/tier2_pitzdaily_ras.txt"
run_tier "tier3" "cylinder / generated gmsh / pisoFoam" "$PROMPT_DIR/tier3_cylinder_gmsh.txt"
run_tier "tier4" "tandem wing / external gmsh import / simpleFoam SA" "$PROMPT_DIR/tier4_tandem_wing_external_mesh.txt" "$FOAM_AGENT/tandem_wing.msh"

echo "all_tiers_finished run_root=$RUN_ROOT logs=$LOG_DIR"

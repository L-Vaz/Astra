#!/usr/bin/env bash
set -uo pipefail

WORKSPACE_EVAL="/opt/Foam-Eval"
LOG_DIR="$WORKSPACE_EVAL/goal_run/logs"
RUN_ROOT="$(cat "$WORKSPACE_EVAL/goal_run/run_root.txt")"

set +u
source /opt/openfoam10/etc/bashrc
set -u
export WM_PROJECT_DIR="${WM_PROJECT_DIR:-/opt/openfoam10}"
export PATH="/opt/openfoam10/platforms/linux64GccDPInt32Opt/bin:/opt/openfoam10/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd "$HOME"

echo "run_root=$RUN_ROOT"
echo "log_dir=$LOG_DIR"

for tier in tier1 tier2 tier3 tier4; do
  case_dir="$RUN_ROOT/$tier"
  echo "===== $tier ====="
  if [ -f "$LOG_DIR/$tier.status" ]; then
    cat "$LOG_DIR/$tier.status"
  else
    echo "status_file=missing"
  fi

  if [ -f "$LOG_DIR/$tier.time" ]; then
    grep -E "Elapsed \\(wall clock\\) time|Maximum resident set size|Exit status" "$LOG_DIR/$tier.time" || true
  fi

  if [ -f "$LOG_DIR/$tier.log" ]; then
    echo "review_loops=$(grep -c '<router>Loop ' "$LOG_DIR/$tier.log" || true)"
    grep -E "Total calls:|Failed calls:|Total retries:|Total prompt tokens:|Total completion tokens:|Total tokens:" "$LOG_DIR/$tier.log" || true
  fi

  if [ -d "$case_dir/constant/polyMesh" ]; then
    check_log="$LOG_DIR/${tier}_independent_checkMesh.log"
    set +e
    checkMesh -case "$case_dir" -allGeometry -allTopology > "$check_log" 2>&1
    rc=$?
    set -e
    echo "independent_checkMesh_exit=$rc"
    grep -E "Mesh OK|Failed|Failed [0-9]+ mesh checks|cells:|faces:|points:|Boundary openness|Max skewness|End" "$check_log" | tail -30 || true
  else
    echo "independent_checkMesh=skipped_no_polyMesh"
  fi

  echo "visualization_files:"
  find "$case_dir" -maxdepth 3 -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.vtk" -o -iname "*.foam" \) -printf "%p\n" 2>/dev/null | sort || true
done

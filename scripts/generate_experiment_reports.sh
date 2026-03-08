#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/generate_experiment_reports.sh
#   scripts/generate_experiment_reports.sh /path/to/experiment-data
#
# Default experiment root matches the user's current setup.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPERIMENT_ROOT="${1:-$HOME/crsbench_eval_patch/experiment-data}"
OUTPUT_ROOT="$REPO_ROOT/reports"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed or not on PATH" >&2
  exit 1
fi

# Format: "<output_subdir>|<experiment_id>"
EXPERIMENTS=(
  "codex|afc-all-crs-codex-gpt-5-3-codex-full"
  "copilot-codex|afc-all-crs-copilot-cli-gpt-5-3-codex-full"
  "copilot-gemini|afc-all-crs-copilot-cli-gemini-3-pro-preview-full"
  "copilot-opus|afc-all-crs-copilot-cli-opus-4-6-full"
  "gemini|afc-all-crs-gemini-cli-gemini-3-pro-preview-full"
  "opus-full-all|afc-all-crs-claude-code-opus-4-6-full"
  "opus-full|afc-all-crs-claude-code-opus-4-6-full-main"
)

echo "Experiment root: $EXPERIMENT_ROOT"
echo "Repo root: $REPO_ROOT"

USED_EXPERIMENT_DIRS=()
generated_reports=0

for item in "${EXPERIMENTS[@]}"; do
  IFS='|' read -r -a parts <<< "$item"
  output_subdir="${parts[0]}"
  output_dir="$OUTPUT_ROOT/$output_subdir"

  experiment_id="${parts[1]}"
  experiment_dir="$EXPERIMENT_ROOT/$experiment_id"

  if [[ ! -d "$experiment_dir" ]]; then
    echo "skip: missing experiment dir for output '$output_subdir': $experiment_dir" >&2
    continue
  fi

  already_used=0
  for used_dir in "${USED_EXPERIMENT_DIRS[@]}"; do
    if [[ "$used_dir" == "$experiment_dir" ]]; then
      already_used=1
      break
    fi
  done
  if [[ "$already_used" -eq 1 ]]; then
    echo "skip: already used experiment dir: $experiment_dir (for $output_subdir)" >&2
    continue
  fi
  USED_EXPERIMENT_DIRS+=("$experiment_dir")

  mkdir -p "$output_dir"
  echo "running: uv run crsbench report --experiment-dir $experiment_dir --format csv --output $output_dir"
  (
    cd "$REPO_ROOT"
    uv run crsbench report --experiment-dir "$experiment_dir" --format csv --output "$output_dir"
  )
  ((generated_reports+=1))
done

if [[ "$generated_reports" -eq 0 ]]; then
  echo "error: no reports generated; none of the expected experiment directories were present" >&2
  exit 1
fi

echo "running: python3 $REPO_ROOT/reports/analysis/generate_crs_svgs.py"
(
  cd "$REPO_ROOT"
  python3 reports/analysis/generate_crs_svgs.py
)

echo "running: python3 $REPO_ROOT/reports/analysis/generate_combined_svg.py"
(
  cd "$REPO_ROOT"
  python3 reports/analysis/generate_combined_svg.py
)

echo "done"

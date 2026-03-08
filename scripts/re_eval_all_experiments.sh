#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/re_eval_all_experiments.sh
#   scripts/re_eval_all_experiments.sh /path/to/experiment-configs
#   scripts/re_eval_all_experiments.sh /path/to/experiment-configs /path/to/experiment-data

CONFIG_ROOT="${1:-experiment-configs}"
EXPERIMENT_ROOT="${2:-$HOME/crsbench_eval_patch/experiment-data}"
CONFIG_GLOB="experiment-config-afc-all-crs-*.yaml"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed or not on PATH" >&2
  exit 1
fi

if [[ ! -d "$CONFIG_ROOT" ]]; then
  echo "error: config root does not exist: $CONFIG_ROOT" >&2
  exit 1
fi

if [[ ! -d "$EXPERIMENT_ROOT" ]]; then
  echo "error: experiment root does not exist: $EXPERIMENT_ROOT" >&2
  exit 1
fi

CONFIGS=()
while IFS= read -r config_path; do
  CONFIGS+=("$config_path")
done < <(find "$CONFIG_ROOT" -maxdepth 1 -type f -name "$CONFIG_GLOB" | sort)

if [[ ${#CONFIGS[@]} -eq 0 ]]; then
  echo "error: no configs matched $CONFIG_ROOT/$CONFIG_GLOB" >&2
  exit 1
fi

echo "Config root: $CONFIG_ROOT"
echo "Experiment root: $EXPERIMENT_ROOT"
echo "Matched configs: ${#CONFIGS[@]}"

matched_existing=0
for config in "${CONFIGS[@]}"; do
  config_file="$(basename "$config")"
  experiment_id="${config_file#experiment-config-}"
  experiment_id="${experiment_id%.yaml}"
  experiment_dir="$EXPERIMENT_ROOT/$experiment_id"

  if [[ ! -d "$experiment_dir" ]]; then
    echo "skip: missing experiment dir for config '$config_file': $experiment_dir" >&2
    continue
  fi

  ((matched_existing+=1))
  echo "running: uv run crsbench re-eval -c $config"
  uv run crsbench re-eval -c "$config"
done

if [[ $matched_existing -eq 0 ]]; then
  echo "error: no matching existing experiment directories found for configs in $CONFIG_ROOT" >&2
  exit 1
fi

echo "done"

#!/usr/bin/env bash
set -euo pipefail

# Run builder-sidecar-lite RTS experiment.
#
# Usage:
#   scripts/run-rts-experiment.sh                          # default config
#   scripts/run-rts-experiment.sh path/to/custom.yaml      # custom config

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG="${1:-experiment-configs/sidecar-test/builder-sidecar-lite-rts.yaml}"

if [[ ! -f "$REPO_ROOT/$CONFIG" ]]; then
  echo "error: config not found: $REPO_ROOT/$CONFIG" >&2
  exit 1
fi

echo "=== RTS Experiment ==="
echo "Config: $CONFIG"
echo "Repo:   $REPO_ROOT"
echo ""

cd "$REPO_ROOT"

# Validate config before running
echo "--- Validating config ---"
uv run crsbench run --experiment-config "$CONFIG" --dry-run 2>&1 || true
echo ""

echo "--- Starting experiment ---"
exec uv run crsbench run --experiment-config "$CONFIG"

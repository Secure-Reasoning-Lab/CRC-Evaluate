#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd -P)"

cd "$REPOSITORY_ROOT"

printf '[%s] Starting Finder\n' "$(date -Is)"
uv run crsbench run \
  --local-only \
  --experiment-config "$SCRIPT_DIR/finder-runtime.yaml"

printf '[%s] Starting Patcher\n' "$(date -Is)"
uv run crsbench run \
  --local-only \
  --experiment-config "$SCRIPT_DIR/patcher-runtime.yaml"

printf '[%s] Sanity workflow completed\n' "$(date -Is)"

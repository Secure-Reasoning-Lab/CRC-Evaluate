#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_DIR="${CRSBENCH_LOCAL_REHEARSAL_STATE_DIR:-${REPO_ROOT}/.crsbench-local-rehearsal}"
EXPERIMENT_CONFIG="${CRSBENCH_LOCAL_REHEARSAL_EXPERIMENT_CONFIG:-${SCRIPT_DIR}/local-experiment.yaml}"

mkdir -p "${STATE_DIR}"
rm -rf "${STATE_DIR}/metadata" "${STATE_DIR}/state"

uv run python "${SCRIPT_DIR}/render_metadata.py" \
  --output-dir "${STATE_DIR}" \
  --experiment-config "${EXPERIMENT_CONFIG}" \
  --repo-mount-path /src/CRSBench \
  --source-repo-path "${REPO_ROOT}" \
  --worker-count 2

export CRSBENCH_LOCAL_REHEARSAL_REPO_ROOT="${REPO_ROOT}"
export CRSBENCH_LOCAL_REHEARSAL_STATE_DIR="${STATE_DIR}"

if [[ $# -eq 0 ]]; then
  docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up --build --remove-orphans
  exit 0
fi

docker compose -f "${SCRIPT_DIR}/docker-compose.yml" "$@"

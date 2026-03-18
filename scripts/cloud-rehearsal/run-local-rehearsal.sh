#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_DIR="${CRSBENCH_LOCAL_REHEARSAL_STATE_DIR:-${REPO_ROOT}/.crsbench-local-rehearsal}"
EXPERIMENT_CONFIG="${CRSBENCH_LOCAL_REHEARSAL_EXPERIMENT_CONFIG:-${SCRIPT_DIR}/local-experiment.yaml}"
GIT_REF="${CRSBENCH_LOCAL_REHEARSAL_GIT_REF:-}"

mkdir -p "${STATE_DIR}"

export CRSBENCH_LOCAL_REHEARSAL_REPO_ROOT="${REPO_ROOT}"
export CRSBENCH_LOCAL_REHEARSAL_STATE_DIR="${STATE_DIR}"

docker_cleanup_state() {
  docker run --rm -v "${STATE_DIR}:/state" alpine:3.20 sh -euc "
    rm -rf /state/metadata /state/state
    mkdir -p /state/metadata /state/state
    chown -R $(id -u):$(id -g) /state
  "
}

reset_compose_runtime() {
  docker compose -f "${SCRIPT_DIR}/docker-compose.yml" down --remove-orphans >/dev/null 2>&1 || true
}

reset_local_state() {
  if ! rm -rf "${STATE_DIR}/metadata" "${STATE_DIR}/state"; then
    docker_cleanup_state
    return 0
  fi
}

render_metadata() {
  local -a cmd=(
    uv run python "${SCRIPT_DIR}/render_metadata.py"
    --output-dir "${STATE_DIR}"
    --experiment-config "${EXPERIMENT_CONFIG}"
    --repo-mount-path /src/CRSBench
    --source-repo-path "${REPO_ROOT}"
    --worker-count 2
    --evaluator-count 1
  )
  if [[ -n "${GIT_REF}" ]]; then
    cmd+=(--git-ref "${GIT_REF}")
  fi
  "${cmd[@]}"
}

if [[ $# -eq 0 ]]; then
  reset_compose_runtime
  reset_local_state
  render_metadata
  docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up --build --remove-orphans
  exit 0
fi

if [[ "$1" == "up" ]]; then
  reset_compose_runtime
  reset_local_state
  render_metadata
fi

docker compose -f "${SCRIPT_DIR}/docker-compose.yml" "$@"

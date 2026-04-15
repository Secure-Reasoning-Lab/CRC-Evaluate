#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WRAPPER="${SCRIPT_DIR}/run-local-rehearsal.sh"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
EXPERIMENT_CONFIG="${SCRIPT_DIR}/local-experiment-notification.yaml"
STATE_DIR="${CRSBENCH_LOCAL_REHEARSAL_STATE_DIR:-${REPO_ROOT}/.crsbench-local-rehearsal}"
WAIT_TIMEOUT_SECONDS="${CRSBENCH_NOTIFICATION_REHEARSAL_WAIT_TIMEOUT_SECONDS:-120}"
DRY_RUN=1
KEEP_UP=0

require_notification_urls() {
  if [[ -z "${CRSBENCH_NOTIFY_APPRISE_URLS:-}" ]]; then
    echo "Error: CRSBENCH_NOTIFY_APPRISE_URLS must be set before running the notification rehearsal." >&2
    exit 1
  fi
}

wait_for_orchestrator() {
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
  local last_error=""
  while true; do
    if last_error="$(docker compose -f "${COMPOSE_FILE}" exec -T orchestrator true 2>&1 >/dev/null)"; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "Error: timed out waiting for the orchestrator container to become ready." >&2
      if [[ -n "${last_error}" ]]; then
        echo "Last docker compose exec error: ${last_error}" >&2
      fi
      exit 1
    fi
    sleep 1
  done
}

require_rendered_metadata() {
  local metadata_file="${STATE_DIR}/metadata/orchestrator/attributes/crsbench-env-passthrough-b64"
  if [[ ! -f "${metadata_file}" ]]; then
    echo "Error: expected rendered orchestrator metadata at ${metadata_file}, but the file does not exist." >&2
    exit 1
  fi

  python - "${metadata_file}" <<'PY'
from __future__ import annotations

import base64
import json
import pathlib
import sys

metadata_file = pathlib.Path(sys.argv[1])
try:
    encoded = metadata_file.read_text(encoding="utf-8").strip()
except OSError as exc:
    print(
        f"Error: unable to read rendered orchestrator metadata at {metadata_file}: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

try:
    decoded = base64.b64decode(encoded).decode("utf-8")
    env_map = json.loads(decoded)
except (ValueError, json.JSONDecodeError) as exc:
    print(
        f"Error: rendered orchestrator metadata at {metadata_file} is not valid base64-encoded JSON: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

if "CRSBENCH_NOTIFY_APPRISE_URLS" not in env_map or not env_map["CRSBENCH_NOTIFY_APPRISE_URLS"]:
    print(
        "Error: rendered orchestrator metadata does not include CRSBENCH_NOTIFY_APPRISE_URLS.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

wait_for_orchestrator_runtime_env() {
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
  local last_error=""
  while true; do
    if last_error="$(
      docker compose -f "${COMPOSE_FILE}" exec -T orchestrator bash -lc \
        'test -s /var/lib/crsbench/orchestrator.env && grep -q "^CRSBENCH_NOTIFY_APPRISE_URLS=" /var/lib/crsbench/orchestrator.env && grep -q "^PATH=" /var/lib/crsbench/orchestrator.env' \
        2>&1 >/dev/null
    )"; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "Error: timed out waiting for orchestrator runtime env at /var/lib/crsbench/orchestrator.env with notification URLs and PATH populated." >&2
      if [[ -n "${last_error}" ]]; then
        echo "Last docker compose exec error: ${last_error}" >&2
      fi
      exit 1
    fi
    sleep 1
  done
}

run_smoke_test() {
  local dry_run_suffix=""
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    dry_run_suffix=" --dry-run"
  fi

  docker compose -f "${COMPOSE_FILE}" exec -T orchestrator bash -lc \
    "set -a && source /var/lib/crsbench/orchestrator.env && set +a && cd /src/CRSBench && python scripts/test_notification.py --no-dotenv${dry_run_suffix}"
}

cleanup() {
  local exit_status=$?
  local teardown_status=0
  trap - EXIT INT TERM
  if [[ "${KEEP_UP}" -eq 0 ]]; then
    if "${WRAPPER}" down -v >/dev/null 2>&1; then
      teardown_status=0
    else
      teardown_status=$?
      echo "Error: failed to tear down the local notification rehearsal stack." >&2
      if [[ "${exit_status}" -eq 0 ]]; then
        exit_status=${teardown_status}
      fi
    fi
  fi
  exit "${exit_status}"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --send)
        DRY_RUN=0
        ;;
      --keep-up)
        KEEP_UP=1
        ;;
      *)
        echo "Error: unknown argument: $1" >&2
        exit 1
        ;;
    esac
    shift
  done

  require_notification_urls

  export CRSBENCH_LOCAL_REHEARSAL_EXPERIMENT_CONFIG="${EXPERIMENT_CONFIG}"
  export CRSBENCH_LOCAL_REHEARSAL_REPO_ROOT="${REPO_ROOT}"
  export CRSBENCH_LOCAL_REHEARSAL_STATE_DIR="${STATE_DIR}"

  trap cleanup EXIT INT TERM

  "${WRAPPER}" up -d
  wait_for_orchestrator
  require_rendered_metadata
  wait_for_orchestrator_runtime_env
  run_smoke_test
}

main "$@"

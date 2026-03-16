#!/usr/bin/env bash
set -euo pipefail

INSTANCE_METADATA_BASE="http://metadata.google.internal/computeMetadata/v1/instance"
ATTRIBUTE_METADATA_BASE="${INSTANCE_METADATA_BASE}/attributes"
METADATA_HEADER="Metadata-Flavor: Google"
STATE_DIR="/var/lib/crsbench"
PAYLOAD_PATH="${STATE_DIR}/bootstrap.json"
LAUNCHER_PATH="${STATE_DIR}/launch-worker.sh"
ENV_PATH="/etc/default/crsbench-worker"
SERVICE_PATH="/etc/systemd/system/crsbench-worker.service"

metadata_get() {
  curl -fsS -H "${METADATA_HEADER}" "${ATTRIBUTE_METADATA_BASE}/$1"
}

metadata_get_optional() {
  curl -fsS -H "${METADATA_HEADER}" "${ATTRIBUTE_METADATA_BASE}/$1" 2>/dev/null || true
}

instance_metadata_get() {
  curl -fsS -H "${METADATA_HEADER}" "${INSTANCE_METADATA_BASE}/$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

write_env_var() {
  printf "%s=%q\n" "$1" "$2" >> "${ENV_PATH}"
}

report_bootstrap_failure() {
  local evidence="$1"
  python3 - "${CRSBENCH_REDIS_HOST:-}" "${evidence}" <<'PY' || true
import sys

try:
    from crsbench.cloud.runtime import report_cloud_worker_state_from_env
except Exception:
    raise SystemExit(0)

redis_host = sys.argv[1]
if not redis_host:
    raise SystemExit(0)

report_cloud_worker_state_from_env(
    redis_host=redis_host,
    state="bootstrap_failed",
    detail="GCE worker bootstrap failed",
    startup_evidence=sys.argv[2],
)
PY
}

on_error() {
  report_bootstrap_failure "startup script failed at line $1: $2"
}

require_cmd curl
require_cmd python3
require_cmd systemctl

mkdir -p "${STATE_DIR}"
metadata_get "crsbench-bootstrap-payload" | base64 --decode > "${PAYLOAD_PATH}"

readarray -t PAYLOAD_FIELDS < <(
  python3 - "${PAYLOAD_PATH}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

print(payload["redis_host"])
print(payload["worker_name"])
print(payload["experiment"])
print(payload.get("worker_jobs") or "")
print(payload.get("worker_cores_per_job") or "")
print(payload.get("worker_cpu_tag") or "")
PY
)

REDIS_HOST="${PAYLOAD_FIELDS[0]}"
WORKER_NAME="${PAYLOAD_FIELDS[1]}"
EXPERIMENT_NAME="${PAYLOAD_FIELDS[2]}"
WORKER_JOBS="${PAYLOAD_FIELDS[3]}"
WORKER_CORES_PER_JOB="${PAYLOAD_FIELDS[4]}"
WORKER_CPU_TAG="${PAYLOAD_FIELDS[5]}"
INSTANCE_ID="$(instance_metadata_get "id")"
ZONE_PATH="$(instance_metadata_get "zone")"
ZONE="${ZONE_PATH##*/}"

export CRSBENCH_REDIS_HOST="${REDIS_HOST}"
export CRSBENCH_WORKER_NAME="${WORKER_NAME}"
export CRSBENCH_EXPERIMENT_NAME="${EXPERIMENT_NAME}"
export CRSBENCH_WORKER_JOBS="${WORKER_JOBS}"
export CRSBENCH_WORKER_CORES_PER_JOB="${WORKER_CORES_PER_JOB}"
export CRSBENCH_WORKER_CPU_TAG="${WORKER_CPU_TAG}"
export CRSBENCH_CLOUD_EXPERIMENT="${EXPERIMENT_NAME}"
export CRSBENCH_CLOUD_INSTANCE_ID="${INSTANCE_ID}"
export CRSBENCH_CLOUD_INSTANCE_NAME="${WORKER_NAME}"
export CRSBENCH_CLOUD_ZONE="${ZONE}"

trap 'on_error "${LINENO}" "${BASH_COMMAND}"' ERR

INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
GITHUB_DEPLOY_KEY="$(metadata_get_optional "crsbench-github-deploy-key")"
HF_TOKEN="$(metadata_get_optional "crsbench-hf-token")"

# --- GitHub SSH setup (if deploy key provided) ---
if [[ -n "${GITHUB_DEPLOY_KEY}" ]]; then
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  echo "${GITHUB_DEPLOY_KEY}" | base64 --decode > /root/.ssh/id_ed25519
  chmod 600 /root/.ssh/id_ed25519
  ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
  git config --global url."git@github.com:sslab-gatech/".insteadOf "https://github.com/sslab-gatech/"
fi

# --- HuggingFace token ---
if [[ -n "${HF_TOKEN}" ]]; then
  export HF_TOKEN
fi

# --- Install crsbench ---
if ! command -v crsbench >/dev/null 2>&1; then
  if [[ -z "${INSTALL_SPEC}" ]]; then
    echo "crsbench CLI not found and no crsbench-install-spec metadata provided" >&2
    exit 1
  elif [[ "${INSTALL_SPEC}" == git+ssh://* ]]; then
    # Private repo clone path
    REPO_URL="${INSTALL_SPEC#git+ssh://}"
    CLONE_DIR="/opt/crsbench"
    git clone "ssh://${REPO_URL}" "${CLONE_DIR}"
    cd "${CLONE_DIR}"
    git submodule update --init --recursive
    # Install uv
    if ! command -v uv >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="/root/.local/bin:${PATH}"
    fi
    uv sync --all-extras
    uv pip install -e .
    cd /
  else
    python3 -m pip install --upgrade "${INSTALL_SPEC}"
  fi
fi

: > "${ENV_PATH}"
write_env_var "CRSBENCH_REDIS_HOST" "${REDIS_HOST}"
write_env_var "CRSBENCH_WORKER_NAME" "${WORKER_NAME}"
write_env_var "CRSBENCH_EXPERIMENT_NAME" "${EXPERIMENT_NAME}"
write_env_var "CRSBENCH_WORKER_JOBS" "${WORKER_JOBS}"
write_env_var "CRSBENCH_WORKER_CORES_PER_JOB" "${WORKER_CORES_PER_JOB}"
write_env_var "CRSBENCH_WORKER_CPU_TAG" "${WORKER_CPU_TAG}"
write_env_var "CRSBENCH_CLOUD_EXPERIMENT" "${EXPERIMENT_NAME}"
write_env_var "CRSBENCH_CLOUD_INSTANCE_ID" "${INSTANCE_ID}"
write_env_var "CRSBENCH_CLOUD_INSTANCE_NAME" "${WORKER_NAME}"
write_env_var "CRSBENCH_CLOUD_ZONE" "${ZONE}"
write_env_var "CRSBENCH_LOG_LEVEL" "INFO"
if [[ -n "${HF_TOKEN:-}" ]]; then
  write_env_var "HF_TOKEN" "${HF_TOKEN}"
fi

cat > "${LAUNCHER_PATH}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

report_bootstrap_failure() {
  local evidence="$1"
  python3 - "${CRSBENCH_REDIS_HOST:-}" "${evidence}" <<'PY' || true
import sys

try:
    from crsbench.cloud.runtime import report_cloud_worker_state_from_env
except Exception:
    raise SystemExit(0)

redis_host = sys.argv[1]
if not redis_host:
    raise SystemExit(0)

report_cloud_worker_state_from_env(
    redis_host=redis_host,
    state="bootstrap_failed",
    detail="GCE worker service failed",
    startup_evidence=sys.argv[2],
)
PY
}

cmd=(
  /usr/bin/env
  crsbench
  worker
  --experiment-name
  "${CRSBENCH_EXPERIMENT_NAME}"
  --worker-name
  "${CRSBENCH_WORKER_NAME}"
)

if [[ -n "${CRSBENCH_WORKER_JOBS:-}" ]]; then
  cmd+=(--jobs "${CRSBENCH_WORKER_JOBS}")
fi

if [[ -n "${CRSBENCH_WORKER_CORES_PER_JOB:-}" ]]; then
  cmd+=(--cores-per-job "${CRSBENCH_WORKER_CORES_PER_JOB}")
fi

if [[ -n "${CRSBENCH_WORKER_CPU_TAG:-}" ]]; then
  cmd+=(--cpu-tag "${CRSBENCH_WORKER_CPU_TAG}")
fi

set +e
"${cmd[@]}"
exit_code="$?"
set -e
if [[ "${exit_code}" -eq 0 ]]; then
  exit 0
fi

report_bootstrap_failure "worker service exited with status ${exit_code}"
exit "${exit_code}"
EOF
chmod +x "${LAUNCHER_PATH}"

cat > "${SERVICE_PATH}" <<'EOF'
[Unit]
Description=CRSBench worker service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/default/crsbench-worker
ExecStart=/bin/bash /var/lib/crsbench/launch-worker.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now crsbench-worker.service

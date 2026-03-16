#!/usr/bin/env bash
# Startup script for a GCE orchestrator VM.
#
# Expected instance metadata:
#   crsbench-install-spec       git+ssh://... or pip spec (required)
#   crsbench-experiment-config-b64  base64-encoded experiment YAML payload (required)
#   crsbench-redis-password     shared Valkey password (required)
#   crsbench-git-ref            branch/tag to clone (default: main)
#   crsbench-github-deploy-key  base64-encoded SSH private key (optional)
#   crsbench-hf-token           HuggingFace token (optional)
#
# The script:
#   1. Clones CRSBench (same git+ssh path as worker.sh)
#   2. Installs Docker and starts Valkey with the shared password on 0.0.0.0:6379
#   3. Decodes the experiment config payload and patches redis_host to localhost:6379
#   4. Marks workers as pre-provisioned so `crsbench run` does not create them again
#   4. Runs `crsbench run`
set -euo pipefail

INSTANCE_METADATA_BASE="http://metadata.google.internal/computeMetadata/v1/instance"
ATTRIBUTE_METADATA_BASE="${INSTANCE_METADATA_BASE}/attributes"
METADATA_HEADER="Metadata-Flavor: Google"
STATE_DIR="/var/lib/crsbench"
LOG_PATH="${STATE_DIR}/orchestrator.log"

metadata_get() {
  curl -fsS -H "${METADATA_HEADER}" "${ATTRIBUTE_METADATA_BASE}/$1"
}

metadata_get_optional() {
  curl -fsS -H "${METADATA_HEADER}" "${ATTRIBUTE_METADATA_BASE}/$1" 2>/dev/null || true
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

require_cmd curl
require_cmd systemctl

mkdir -p "${STATE_DIR}"

# Redirect all output to log file AND console
exec > >(tee -a "${LOG_PATH}") 2>&1

echo "=== CRSBench orchestrator bootstrap started at $(date -u) ==="

# --- Install system packages ---
echo "Installing system packages..."
apt-get update -qq
apt-get install -y -qq git python3 rsync tar

# Docker via official install script (includes docker compose plugin)
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

# --- Read metadata ---
INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
GIT_REF="$(metadata_get_optional "crsbench-git-ref")"
EXPERIMENT_CONFIG_B64="$(metadata_get "crsbench-experiment-config-b64")"
REDIS_PASSWORD="$(metadata_get "crsbench-redis-password")"
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
CLONE_DIR=""
if [[ -z "${INSTALL_SPEC}" ]]; then
  echo "crsbench-install-spec metadata is required for orchestrator" >&2
  exit 1
elif [[ "${INSTALL_SPEC}" == git+ssh://* ]]; then
  REPO_URL="${INSTALL_SPEC#git+ssh://}"
  CLONE_DIR="/opt/crsbench"
  git clone -b "${GIT_REF:-main}" "ssh://${REPO_URL}" "${CLONE_DIR}"
  cd "${CLONE_DIR}"
  git submodule update --init --recursive
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="/root/.local/bin:${PATH}"
  fi
  uv sync --all-extras
  uv pip install -e .
  export PATH="/opt/crsbench/.venv/bin:/root/.local/bin:${PATH}"
else
  python3 -m pip install --upgrade "${INSTALL_SPEC}"
fi

# --- Start Valkey with password auth on 0.0.0.0:6379 ---
echo "Starting Valkey..."
docker run -d \
  --name crsbench-valkey \
  -p "0.0.0.0:6379:6379" \
  -v valkey_valkey-data:/data \
  --restart unless-stopped \
  valkey/valkey:8.0-alpine \
  valkey-server \
  --appendonly yes \
  --requirepass "${REDIS_PASSWORD}"

# Wait for Valkey to be ready
for _i in $(seq 1 30); do
  if docker exec -e "REDISCLI_AUTH=${REDIS_PASSWORD}" crsbench-valkey \
       valkey-cli ping 2>/dev/null | grep -q PONG; then
    echo "Valkey is ready"
    break
  fi
  sleep 1
done

export CRSBENCH_REDIS_PASSWORD="${REDIS_PASSWORD}"
export CRSBENCH_CLOUD_PREPROVISIONED_WORKERS="1"

# --- Decode experiment config and patch redis_host ---
CONFIG_PATH="${STATE_DIR}/experiment-config.yaml"
echo "${EXPERIMENT_CONFIG_B64}" | base64 --decode > "${CONFIG_PATH}"

# Patch redis_host in the config to point to localhost.
python3 - "${CONFIG_PATH}" <<'PY'
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
content = config_path.read_text()
lines = content.split("\n")
patched = []
for line in lines:
    stripped = line.lstrip()
    if stripped.startswith("redis_host:"):
        indent = line[: len(line) - len(stripped)]
        patched.append(f"{indent}redis_host: localhost:6379")
    else:
        patched.append(line)
config_path.write_text("\n".join(patched))
PY

echo "Patched redis_host in ${CONFIG_PATH}"

# --- Run orchestrator ---
echo "=== Starting crsbench run at $(date -u) ==="
echo "Config: ${CONFIG_PATH}"

cd "${CLONE_DIR:-/}"

crsbench run --experiment-config "${CONFIG_PATH}"

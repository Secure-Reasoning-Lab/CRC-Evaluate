#!/usr/bin/env bash
# Startup script for a GCE orchestrator VM.
#
# Expected instance metadata:
#   crsbench-install-spec       git+https://..., git+ssh://..., or pip spec (required)
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

CRSBENCH_METADATA_ROOT_DIR="${CRSBENCH_METADATA_ROOT_DIR:-}"
CRSBENCH_METADATA_BASE_URL="${CRSBENCH_METADATA_BASE_URL:-http://metadata.google.internal/computeMetadata/v1}"
CRSBENCH_METADATA_HEADER_NAME="${CRSBENCH_METADATA_HEADER_NAME:-Metadata-Flavor}"
CRSBENCH_METADATA_HEADER_VALUE="${CRSBENCH_METADATA_HEADER_VALUE:-Google}"
STATE_DIR="${CRSBENCH_STATE_DIR:-/var/lib/crsbench}"
LOG_PATH="${STATE_DIR}/orchestrator.log"
CLONE_DIR="${CRSBENCH_CLONE_DIR:-/opt/crsbench}"
CLONE_GIT_SSH_COMMAND=""

metadata_fetch() {
  local relative_path="$1"
  local optional="${2:-0}"
  if [[ -n "${CRSBENCH_METADATA_ROOT_DIR}" ]]; then
    local metadata_path="${CRSBENCH_METADATA_ROOT_DIR%/}/${relative_path}"
    if [[ -f "${metadata_path}" ]]; then
      cat "${metadata_path}"
      return 0
    fi
    if [[ "${optional}" == "1" ]]; then
      return 1
    fi
    echo "missing metadata file: ${metadata_path}" >&2
    return 1
  fi

  local metadata_url="${CRSBENCH_METADATA_BASE_URL%/}/${relative_path}"
  local -a curl_args=(-fsS)
  if [[ -n "${CRSBENCH_METADATA_HEADER_NAME}" && -n "${CRSBENCH_METADATA_HEADER_VALUE}" ]]; then
    curl_args+=(-H "${CRSBENCH_METADATA_HEADER_NAME}: ${CRSBENCH_METADATA_HEADER_VALUE}")
  fi
  if [[ "${optional}" == "1" ]]; then
    curl "${curl_args[@]}" "${metadata_url}" 2>/dev/null || return 1
    return 0
  fi
  curl "${curl_args[@]}" "${metadata_url}"
}

metadata_get() {
  metadata_fetch "instance/attributes/$1"
}

metadata_get_optional() {
  metadata_fetch "instance/attributes/$1" 1 || true
}

for_each_passthrough_env() {
  local encoded="$1"
  if [[ -z "${encoded}" ]]; then
    return 0
  fi
  python3 - "${encoded}" <<'PY'
import base64
import json
import sys

encoded = sys.argv[1]
if not encoded:
    raise SystemExit(0)

data = json.loads(base64.b64decode(encoded).decode("utf-8"))
for key, value in data.items():
    encoded_value = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
    print(f"{key}\t{encoded_value}")
PY
}

export_passthrough_env() {
  local encoded="$1"
  while IFS=$'\t' read -r env_name env_value_b64; do
    [[ -z "${env_name}" ]] && continue
    local env_value
    env_value="$(printf "%s" "${env_value_b64}" | base64 --decode)"
    export "${env_name}=${env_value}"
  done < <(for_each_passthrough_env "${encoded}")
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq "$@"
    return
  fi
  if command -v apk >/dev/null 2>&1; then
    apk add --no-cache "$@"
    return
  fi
  echo "No supported package manager found (need apt-get or apk)" >&2
  exit 1
}

ensure_system_packages() {
  if command -v git >/dev/null 2>&1 \
    && command -v python3 >/dev/null 2>&1 \
    && command -v rsync >/dev/null 2>&1 \
    && command -v tar >/dev/null 2>&1 \
    && command -v ssh-keyscan >/dev/null 2>&1; then
    return 0
  fi

  echo "Installing system packages..."
  if command -v apt-get >/dev/null 2>&1; then
    install_packages git python3 python3-pip python3-venv python3-yaml rsync tar bash coreutils openssh-client
    return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    install_packages git python3 py3-pip py3-yaml rsync tar bash coreutils openssh-client
    return 0
  fi
  echo "Unsupported base image: cannot install git/python/runtime dependencies" >&2
  exit 1
}

clone_repo() {
  local repo_url="$1"
  local clone_dir="$2"
  if [[ "${repo_url}" == file://* ]]; then
    local repo_path="${repo_url#file://}"
    if [[ "${repo_path}" == localhost/* ]]; then
      repo_path="/${repo_path#localhost/}"
    fi
    if [[ -d "${repo_path}" ]]; then
      git config --global --add safe.directory "${repo_path}" || true
      if [[ -d "${repo_path}/.git" ]]; then
        git config --global --add safe.directory "${repo_path}/.git" || true
      fi
    fi
  fi
  if [[ -n "${CLONE_GIT_SSH_COMMAND}" ]]; then
    GIT_SSH_COMMAND="${CLONE_GIT_SSH_COMMAND}" git clone --no-single-branch "${repo_url}" "${clone_dir}"
    return
  fi
  git clone --no-single-branch "${repo_url}" "${clone_dir}"
}

ensure_docker_ready() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Installing Docker..."
    if command -v apt-get >/dev/null 2>&1; then
      curl -fsSL https://get.docker.com | sh
    elif command -v apk >/dev/null 2>&1; then
      install_packages docker docker-cli-compose
    else
      echo "Docker is required but no supported installation path is available" >&2
      exit 1
    fi
  fi

  if docker info >/dev/null 2>&1; then
    return 0
  fi

  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl enable --now docker || true
  fi

  for _i in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "Docker daemon is unavailable after waiting" >&2
  exit 1
}

require_cmd curl

mkdir -p "${STATE_DIR}"

# Redirect all output to log file AND console
exec > >(tee -a "${LOG_PATH}") 2>&1

echo "=== CRSBench orchestrator bootstrap started at $(date -u) ==="

# --- Install system packages ---
ensure_system_packages
ensure_docker_ready

# --- Read metadata ---
INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
GIT_REF="$(metadata_get_optional "crsbench-git-ref")"
EXPERIMENT_CONFIG_B64="$(metadata_get "crsbench-experiment-config-b64")"
REDIS_PASSWORD="$(metadata_get "crsbench-redis-password")"
GITHUB_DEPLOY_KEY="$(metadata_get_optional "crsbench-github-deploy-key")"
HF_TOKEN="$(metadata_get_optional "crsbench-hf-token")"
ENV_PASSTHROUGH_B64="$(metadata_get_optional "crsbench-env-passthrough-b64")"

# --- GitHub SSH setup (if deploy key provided) ---
if [[ -n "${GITHUB_DEPLOY_KEY}" ]]; then
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  echo "${GITHUB_DEPLOY_KEY}" | base64 --decode > /root/.ssh/id_ed25519
  chmod 600 /root/.ssh/id_ed25519
  ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
  CLONE_GIT_SSH_COMMAND="ssh -F /dev/null -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=/root/.ssh/known_hosts -o StrictHostKeyChecking=yes"
fi

# --- HuggingFace token ---
if [[ -n "${HF_TOKEN}" ]]; then
  export HF_TOKEN
fi
export_passthrough_env "${ENV_PASSTHROUGH_B64}"

# --- Install crsbench from a repo checkout ---
if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then
  echo "cloud orchestrator bootstrap requires git+ install spec metadata" >&2
  exit 1
fi
REPO_URL="${INSTALL_SPEC#git+}"
rm -rf "${CLONE_DIR}"
clone_repo "${REPO_URL}" "${CLONE_DIR}"
cd "${CLONE_DIR}"
git checkout "${GIT_REF:-main}"
git submodule update --init --recursive
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="/root/.local/bin:${PATH}"
fi
uv sync --all-extras
uv pip install -e .
VENV_BIN="${CLONE_DIR}/.venv/bin"
export PATH="${VENV_BIN}:/root/.local/bin:${PATH}"

# --- Decode experiment config and patch redis_host ---
CONFIG_PATH="${STATE_DIR}/experiment-config.yaml"
echo "${EXPERIMENT_CONFIG_B64}" | base64 --decode > "${CONFIG_PATH}"

# Patch redis_host in the config to point to the local Valkey instance.
python3 - "${CONFIG_PATH}" <<'PY'
import sys

try:
    from crsbench.cloud.gce.orchestrator_config import (
        patch_experiment_config_for_local_redis,
    )
except Exception:
    import yaml
    from pathlib import Path

    def patch_experiment_config_for_local_redis(config_path, *, redis_host):
        path = Path(config_path)
        raw_config = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw_config, dict):
            raise ValueError(
                f"Experiment config at {path} must deserialize to a mapping, got "
                f"{type(raw_config).__name__}"
            )

        raw_config["redis_host"] = redis_host

        runtime = raw_config.get("runtime")
        if runtime is None:
            runtime = {}
            raw_config["runtime"] = runtime
        if not isinstance(runtime, dict):
            raise ValueError("Experiment config 'runtime' section must be a mapping")

        runtime_redis = runtime.get("redis")
        if runtime_redis is None:
            runtime_redis = {}
            runtime["redis"] = runtime_redis
        if not isinstance(runtime_redis, dict):
            raise ValueError(
                "Experiment config 'runtime.redis' section must be a mapping"
            )

        runtime_redis["host"] = redis_host
        path.write_text(
            yaml.safe_dump(raw_config, sort_keys=False),
            encoding="utf-8",
        )

patch_experiment_config_for_local_redis(sys.argv[1], redis_host="localhost:6379")
PY

echo "Patched redis_host in ${CONFIG_PATH}"

cd "${CLONE_DIR}"

python3 - "${CONFIG_PATH}" <<'PY'
import sys
from pathlib import Path

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs, run_cloud_vm_bootstrap
from crsbench.run_experiment import load_experiment_config

config = load_experiment_config(Path(sys.argv[1]))
run_cloud_vm_bootstrap(
    CloudVmBootstrapInputs.from_experiment_config(config),
    cwd=Path.cwd(),
)
PY

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

# --- Run orchestrator ---
echo "=== Starting crsbench run at $(date -u) ==="
echo "Config: ${CONFIG_PATH}"

cd "${CLONE_DIR}"

crsbench run --experiment-config "${CONFIG_PATH}"

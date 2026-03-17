#!/usr/bin/env bash
set -euo pipefail

CRSBENCH_METADATA_ROOT_DIR="${CRSBENCH_METADATA_ROOT_DIR:-}"
CRSBENCH_METADATA_BASE_URL="${CRSBENCH_METADATA_BASE_URL:-http://metadata.google.internal/computeMetadata/v1}"
CRSBENCH_METADATA_HEADER_NAME="${CRSBENCH_METADATA_HEADER_NAME:-Metadata-Flavor}"
CRSBENCH_METADATA_HEADER_VALUE="${CRSBENCH_METADATA_HEADER_VALUE:-Google}"
CRSBENCH_SERVICE_MANAGER="${CRSBENCH_SERVICE_MANAGER:-auto}"
CRSBENCH_TIMEZONE="${CRSBENCH_TIMEZONE:-America/New_York}"
STATE_DIR="${CRSBENCH_STATE_DIR:-/var/lib/crsbench}"
PAYLOAD_PATH="${STATE_DIR}/bootstrap.json"
LAUNCHER_PATH="${STATE_DIR}/launch-worker.sh"
ENV_PATH="/etc/default/crsbench-worker"
SERVICE_PATH="/etc/systemd/system/crsbench-worker.service"
CLONE_DIR="${CRSBENCH_CLONE_DIR:-/opt/crsbench}"
DOCKER_DAEMON_CONFIG_PATH="${CRSBENCH_DOCKER_DAEMON_CONFIG_PATH:-/etc/docker/daemon.json}"
DOCKER_CGROUP_DRIVER_OPT="${CRSBENCH_DOCKER_CGROUP_DRIVER_OPT:-native.cgroupdriver=cgroupfs}"
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

instance_metadata_get() {
  metadata_fetch "instance/$1"
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
    && command -v ssh-keyscan >/dev/null 2>&1 \
    && [[ -e /usr/share/zoneinfo/UTC ]]; then
    return 0
  fi

  echo "Installing system packages..."
  if command -v apt-get >/dev/null 2>&1; then
    install_packages git python3 python3-pip python3-venv rsync tar bash coreutils openssh-client tzdata sudo
    return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    install_packages git python3 py3-pip rsync tar bash coreutils openssh-client tzdata sudo
    return 0
  fi
  echo "Unsupported base image: cannot install git/python/runtime dependencies" >&2
  exit 1
}

ensure_timezone() {
  if [[ -z "${CRSBENCH_TIMEZONE}" ]]; then
    return 0
  fi
  local zoneinfo_path="/usr/share/zoneinfo/${CRSBENCH_TIMEZONE}"
  if [[ ! -e "${zoneinfo_path}" ]]; then
    echo "Timezone data not found: ${CRSBENCH_TIMEZONE}" >&2
    exit 1
  fi
  export TZ="${CRSBENCH_TIMEZONE}"
  if command -v timedatectl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    timedatectl set-timezone "${CRSBENCH_TIMEZONE}"
    return 0
  fi
  ln -snf "${zoneinfo_path}" /etc/localtime
  printf '%s\n' "${CRSBENCH_TIMEZONE}" > /etc/timezone
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

wait_for_docker() {
  for _i in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_docker_service() {
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl enable --now docker || true
    return 0
  fi
  if command -v service >/dev/null 2>&1; then
    service docker start >/dev/null 2>&1 || true
  fi
}

restart_docker_service() {
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl restart docker
    return 0
  fi
  if command -v service >/dev/null 2>&1; then
    service docker restart
    return 0
  fi
  echo "Docker cgroup driver update requires a supported restart path" >&2
  return 1
}

ensure_docker_cgroupfs() {
  local current_driver
  current_driver="$(docker info --format '{{.CgroupDriver}}' 2>/dev/null || true)"
  if [[ "${current_driver}" == "cgroupfs" ]]; then
    return 0
  fi

  mkdir -p "$(dirname "${DOCKER_DAEMON_CONFIG_PATH}")"
  local changed
  changed="$(
    python3 - "${DOCKER_DAEMON_CONFIG_PATH}" "${DOCKER_CGROUP_DRIVER_OPT}" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1])
desired_opt = sys.argv[2]
config: dict[str, object]
if config_path.exists():
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid Docker daemon config at {config_path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(loaded, dict):
        print(
            f"Invalid Docker daemon config at {config_path}: expected JSON object",
            file=sys.stderr,
        )
        raise SystemExit(2)
    config = loaded
else:
    config = {}

exec_opts = config.get("exec-opts", [])
if exec_opts is None:
    exec_opts = []
if not isinstance(exec_opts, list):
    print(
        f"Invalid Docker daemon config at {config_path}: exec-opts must be a list",
        file=sys.stderr,
    )
    raise SystemExit(2)

normalized = [str(item) for item in exec_opts if str(item) != desired_opt]
normalized.append(desired_opt)
config["exec-opts"] = normalized
rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"

current = None
if config_path.exists():
    current = config_path.read_text(encoding="utf-8")
if current == rendered:
    print("0")
    raise SystemExit(0)

fd, tmp_path = tempfile.mkstemp(prefix=f"{config_path.name}.", dir=config_path.parent)
os.close(fd)
tmp_file = Path(tmp_path)
tmp_file.write_text(rendered, encoding="utf-8")
os.replace(tmp_file, config_path)
print("1")
PY
  )"

  if [[ "${changed}" == "1" ]]; then
    restart_docker_service
  fi
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

  if ! docker info >/dev/null 2>&1; then
    start_docker_service
    if ! wait_for_docker; then
      echo "Docker daemon is unavailable after waiting" >&2
      exit 1
    fi
  fi

  ensure_docker_cgroupfs
  if ! wait_for_docker; then
    echo "Docker daemon is unavailable after waiting" >&2
    exit 1
  fi

  local final_driver
  final_driver="$(docker info --format '{{.CgroupDriver}}' 2>/dev/null || true)"
  if [[ "${final_driver}" != "cgroupfs" ]]; then
    echo "Docker must use the cgroupfs cgroup driver for oss-crs" >&2
    exit 1
  fi
}

supports_systemd() {
  command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]
}

start_worker_runtime() {
  case "${CRSBENCH_SERVICE_MANAGER}" in
    auto)
      if supports_systemd; then
        systemctl daemon-reload
        systemctl enable --now crsbench-worker.service
        return 0
      fi
      ;;
    systemd)
      if ! supports_systemd; then
        echo "CRSBENCH_SERVICE_MANAGER=systemd requires a running systemd host" >&2
        exit 1
      fi
      systemctl daemon-reload
      systemctl enable --now crsbench-worker.service
      return 0
      ;;
    foreground)
      ;;
    *)
      echo "Unsupported CRSBENCH_SERVICE_MANAGER: ${CRSBENCH_SERVICE_MANAGER}" >&2
      exit 1
      ;;
  esac

  # shellcheck disable=SC1090
  set -a
  source "${ENV_PATH}"
  set +a
  exec /bin/bash "${LAUNCHER_PATH}"
}

write_env_var() {
  printf "%s=%q\n" "$1" "$2" >> "${ENV_PATH}"
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

write_passthrough_env_vars() {
  local encoded="$1"
  while IFS=$'\t' read -r env_name env_value_b64; do
    [[ -z "${env_name}" ]] && continue
    local env_value
    env_value="$(printf "%s" "${env_value_b64}" | base64 --decode)"
    write_env_var "${env_name}" "${env_value}"
  done < <(for_each_passthrough_env "${encoded}")
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

mkdir -p "${STATE_DIR}"

# --- Install system packages ---
ensure_system_packages
ensure_timezone
ensure_docker_ready

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
print(payload.get("readiness_timeout_sec") or "")
PY
)

REDIS_HOST="${PAYLOAD_FIELDS[0]}"
WORKER_NAME="${PAYLOAD_FIELDS[1]}"
EXPERIMENT_NAME="${PAYLOAD_FIELDS[2]}"
WORKER_JOBS="${PAYLOAD_FIELDS[3]}"
WORKER_CORES_PER_JOB="${PAYLOAD_FIELDS[4]}"
WORKER_CPU_TAG="${PAYLOAD_FIELDS[5]}"
READINESS_TIMEOUT_SEC="${PAYLOAD_FIELDS[6]}"
REDIS_PASSWORD="$(metadata_get_optional "crsbench-redis-password")"
INSTANCE_ID="$(instance_metadata_get "id")"
ZONE_PATH="$(instance_metadata_get "zone")"
ZONE="${ZONE_PATH##*/}"

export CRSBENCH_REDIS_HOST="${REDIS_HOST}"
export CRSBENCH_REDIS_PASSWORD="${REDIS_PASSWORD}"
export CRSBENCH_WORKER_NAME="${WORKER_NAME}"
export CRSBENCH_EXPERIMENT_NAME="${EXPERIMENT_NAME}"
export CRSBENCH_WORKER_JOBS="${WORKER_JOBS}"
export CRSBENCH_WORKER_CORES_PER_JOB="${WORKER_CORES_PER_JOB}"
export CRSBENCH_WORKER_CPU_TAG="${WORKER_CPU_TAG}"
export CRSBENCH_READINESS_TIMEOUT_SEC="${READINESS_TIMEOUT_SEC}"
export CRSBENCH_CLOUD_EXPERIMENT="${EXPERIMENT_NAME}"
export CRSBENCH_CLOUD_INSTANCE_ID="${INSTANCE_ID}"
export CRSBENCH_CLOUD_INSTANCE_NAME="${WORKER_NAME}"
export CRSBENCH_CLOUD_ZONE="${ZONE}"

trap 'on_error "${LINENO}" "${BASH_COMMAND}"' ERR

INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
GIT_REF="$(metadata_get_optional "crsbench-git-ref")"
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
  echo "cloud worker bootstrap requires git+ install spec metadata" >&2
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

python3 - "${PAYLOAD_PATH}" <<'PY'
import json
import sys
from pathlib import Path

from crsbench.cloud.bootstrap import bootstrap_inputs_from_payload, run_cloud_vm_bootstrap

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
run_cloud_vm_bootstrap(
    bootstrap_inputs_from_payload(payload),
    cwd=Path.cwd(),
)
PY

: > "${ENV_PATH}"
write_env_var "CRSBENCH_REDIS_HOST" "${REDIS_HOST}"
if [[ -n "${REDIS_PASSWORD}" ]]; then
  write_env_var "CRSBENCH_REDIS_PASSWORD" "${REDIS_PASSWORD}"
fi
write_env_var "CRSBENCH_WORKER_NAME" "${WORKER_NAME}"
write_env_var "CRSBENCH_EXPERIMENT_NAME" "${EXPERIMENT_NAME}"
write_env_var "CRSBENCH_WORKER_JOBS" "${WORKER_JOBS}"
write_env_var "CRSBENCH_WORKER_CORES_PER_JOB" "${WORKER_CORES_PER_JOB}"
write_env_var "CRSBENCH_WORKER_CPU_TAG" "${WORKER_CPU_TAG}"
write_env_var "CRSBENCH_READINESS_TIMEOUT_SEC" "${READINESS_TIMEOUT_SEC}"
write_env_var "CRSBENCH_CLOUD_EXPERIMENT" "${EXPERIMENT_NAME}"
write_env_var "CRSBENCH_CLOUD_INSTANCE_ID" "${INSTANCE_ID}"
write_env_var "CRSBENCH_CLOUD_INSTANCE_NAME" "${WORKER_NAME}"
write_env_var "CRSBENCH_CLOUD_ZONE" "${ZONE}"
write_env_var "CRSBENCH_LOG_LEVEL" "INFO"
if [[ -n "${HF_TOKEN:-}" ]]; then
  write_env_var "HF_TOKEN" "${HF_TOKEN}"
fi
write_passthrough_env_vars "${ENV_PASSTHROUGH_B64}"
if [[ -n "${VENV_BIN:-}" ]]; then
  write_env_var "PATH" "${VENV_BIN}:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
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

wait_for_redis() {
  local timeout_sec="${CRSBENCH_READINESS_TIMEOUT_SEC:-900}"
  local poll_interval_sec=5
  local start_time="${SECONDS}"

  if [[ -z "${CRSBENCH_REDIS_HOST:-}" ]]; then
    report_bootstrap_failure "Timed out waiting for Redis: CRSBENCH_REDIS_HOST is unset"
    return 1
  fi

  echo "Waiting for Redis at ${CRSBENCH_REDIS_HOST} for up to ${timeout_sec}s..."
  while true; do
    local probe_output=""
    local probe_exit=0
    local probe_output_file
    probe_output_file="$(mktemp)"
    set +e
    /usr/bin/env python3 - "${CRSBENCH_REDIS_HOST}" >"${probe_output_file}" 2>&1 <<'PY'
import sys

from crsbench.distributed.queue import RedisConnectionProbe, probe_redis_connection

probe_state, detail = probe_redis_connection(sys.argv[1], timeout=2)
if probe_state is RedisConnectionProbe.READY:
    raise SystemExit(0)
if probe_state is RedisConnectionProbe.RETRYABLE:
    raise SystemExit(1)
print(detail or "Redis bootstrap probe failed", file=sys.stderr)
raise SystemExit(2)
PY
    probe_exit="$?"
    set -e
    probe_output="$(cat "${probe_output_file}")"
    rm -f "${probe_output_file}"
    if [[ "${probe_exit}" -eq 0 ]]; then
      echo "Redis at ${CRSBENCH_REDIS_HOST} is ready"
      return 0
    fi
    if [[ "${probe_exit}" -eq 2 ]]; then
      report_bootstrap_failure \
        "Fatal Redis bootstrap error for ${CRSBENCH_REDIS_HOST}: ${probe_output}"
      return 1
    fi

    if (( SECONDS - start_time >= timeout_sec )); then
      report_bootstrap_failure \
        "Timed out waiting for Redis at ${CRSBENCH_REDIS_HOST} after ${timeout_sec}s"
      return 1
    fi
    sleep "${poll_interval_sec}"
  done
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

wait_for_redis

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

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=CRSBench worker service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/default/crsbench-worker
WorkingDirectory=${CLONE_DIR}
ExecStart=/bin/bash ${LAUNCHER_PATH}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

start_worker_runtime

#!/usr/bin/env bash
set -euo pipefail

CRSBENCH_STARTUP_MODE="${CRSBENCH_STARTUP_MODE:-worker}"
CRSBENCH_METADATA_ROOT_DIR="${CRSBENCH_METADATA_ROOT_DIR:-}"
CRSBENCH_METADATA_BASE_URL="${CRSBENCH_METADATA_BASE_URL:-http://metadata.google.internal/computeMetadata/v1}"
CRSBENCH_METADATA_HEADER_NAME="${CRSBENCH_METADATA_HEADER_NAME:-Metadata-Flavor}"
CRSBENCH_METADATA_HEADER_VALUE="${CRSBENCH_METADATA_HEADER_VALUE:-Google}"
CRSBENCH_SERVICE_MANAGER="${CRSBENCH_SERVICE_MANAGER:-auto}"
CRSBENCH_TIMEZONE="${CRSBENCH_TIMEZONE:-America/New_York}"
CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"
CRSBENCH_USER_HOME="${CRSBENCH_USER_HOME:-/home/${CRSBENCH_USER}}"
STATE_DIR="${CRSBENCH_STATE_DIR:-/var/lib/crsbench}"
PAYLOAD_PATH="${STATE_DIR}/bootstrap.json"
LAUNCHER_PATH="${STATE_DIR}/launch-worker.sh"
ENV_PATH="${STATE_DIR}/worker.env"
EXPERIMENT_CONFIG_PATH="${STATE_DIR}/experiment-config.yaml"
CLONE_DIR="${CRSBENCH_CLONE_DIR:-/opt/crsbench}"
DOCKER_DAEMON_CONFIG_PATH="${CRSBENCH_DOCKER_DAEMON_CONFIG_PATH:-/etc/docker/daemon.json}"
DOCKER_CGROUP_DRIVER_OPT="${CRSBENCH_DOCKER_CGROUP_DRIVER_OPT:-native.cgroupdriver=cgroupfs}"
CRSBENCH_BUILDX_BUILDER_NAME="${CRSBENCH_BUILDX_BUILDER_NAME:-crsbuilder}"
USER_SERVICE_DIR="${CRSBENCH_USER_HOME}/.config/systemd/user"
SERVICE_PATH="${USER_SERVICE_DIR}/crsbench-worker.service"
CLONE_GIT_SSH_COMMAND=""
CRSBENCH_USER_UID=""
CRSBENCH_USER_GID=""
CRSBENCH_USER_RUNTIME_DIR=""
CRSBENCH_USER_DBUS_ADDRESS=""
CRSBENCH_USER_LOCAL_BIN=""
CRSBENCH_USER_PATH=""
CRSBENCH_USER_SERVICE_CGROUP=""
CRSBENCH_OSS_CRS_CGROUP=""

if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  LAUNCHER_PATH="${STATE_DIR}/launch-evaluator.sh"
  ENV_PATH="${STATE_DIR}/evaluator.env"
  SERVICE_PATH="${USER_SERVICE_DIR}/crsbench-evaluator.service"
fi

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
  local have_fd_find=1
  if command -v fdfind >/dev/null 2>&1 || command -v fd >/dev/null 2>&1; then
    have_fd_find=0
  fi
  if command -v git >/dev/null 2>&1 \
    && command -v python3 >/dev/null 2>&1 \
    && command -v rsync >/dev/null 2>&1 \
    && command -v tar >/dev/null 2>&1 \
    && command -v rg >/dev/null 2>&1 \
    && [[ "${have_fd_find}" -eq 0 ]] \
    && command -v ssh-keyscan >/dev/null 2>&1 \
    && command -v sudo >/dev/null 2>&1 \
    && [[ -e /usr/share/zoneinfo/UTC ]]; then
    return 0
  fi

  echo "Installing system packages..."
  if command -v apt-get >/dev/null 2>&1; then
    install_packages git python3 python3-pip python3-venv rsync tar bash coreutils openssh-client tzdata sudo ripgrep fd-find
    return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    install_packages git python3 py3-pip rsync tar bash coreutils openssh-client tzdata sudo shadow ripgrep fd
    return 0
  fi
  echo "Unsupported base image: cannot install git/python/runtime dependencies" >&2
  exit 1
}

ensure_user_systemd_support_packages() {
  if ! service_manager_uses_systemd; then
    return 0
  fi
  if ! supports_systemd; then
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    if dpkg-query -W -f='${Status}' dbus-user-session 2>/dev/null | grep -q "install ok installed"; then
      return 0
    fi
    install_packages dbus-user-session
    return 0
  fi

  if command -v apk >/dev/null 2>&1; then
    install_packages dbus
  fi
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
  if ! command -v docker >/dev/null 2>&1 || \
     ! docker compose version >/dev/null 2>&1 || \
     ! docker buildx version >/dev/null 2>&1; then
    echo "Installing Docker..."
    if command -v apt-get >/dev/null 2>&1; then
      install_packages docker.io docker-compose-v2 docker-buildx
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

service_manager_uses_systemd() {
  case "${CRSBENCH_SERVICE_MANAGER}" in
    auto)
      supports_systemd
      ;;
    systemd)
      return 0
      ;;
    foreground)
      return 1
      ;;
    *)
      echo "Unsupported CRSBENCH_SERVICE_MANAGER: ${CRSBENCH_SERVICE_MANAGER}" >&2
      exit 1
      ;;
  esac
}

lookup_user_home() {
  python3 - "$1" <<'PY'
import pwd
import sys

print(pwd.getpwnam(sys.argv[1]).pw_dir)
PY
}

ensure_crsbench_user() {
  if ! id -u "${CRSBENCH_USER}" >/dev/null 2>&1; then
    if command -v useradd >/dev/null 2>&1; then
      useradd --create-home --home-dir "${CRSBENCH_USER_HOME}" --shell /bin/bash "${CRSBENCH_USER}"
    elif command -v adduser >/dev/null 2>&1; then
      adduser -D -h "${CRSBENCH_USER_HOME}" -s /bin/bash "${CRSBENCH_USER}"
    else
      echo "No supported user creation tool found (need useradd or adduser)" >&2
      exit 1
    fi
  fi

  CRSBENCH_USER_HOME="$(lookup_user_home "${CRSBENCH_USER}")"
  USER_SERVICE_DIR="${CRSBENCH_USER_HOME}/.config/systemd/user"
  SERVICE_PATH="${USER_SERVICE_DIR}/crsbench-worker.service"
  if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
    SERVICE_PATH="${USER_SERVICE_DIR}/crsbench-evaluator.service"
  fi
  CRSBENCH_USER_UID="$(id -u "${CRSBENCH_USER}")"
  CRSBENCH_USER_GID="$(id -g "${CRSBENCH_USER}")"
  CRSBENCH_USER_RUNTIME_DIR="/run/user/${CRSBENCH_USER_UID}"
  CRSBENCH_USER_DBUS_ADDRESS="unix:path=${CRSBENCH_USER_RUNTIME_DIR}/bus"
  CRSBENCH_USER_LOCAL_BIN="${CRSBENCH_USER_HOME}/.local/bin"
  CRSBENCH_USER_PATH="${CRSBENCH_USER_LOCAL_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  CRSBENCH_USER_SERVICE_CGROUP="/sys/fs/cgroup/user.slice/user-${CRSBENCH_USER_UID}.slice/user@${CRSBENCH_USER_UID}.service"
  CRSBENCH_RUNTIME_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/crsbench"
  CRSBENCH_OSS_CRS_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/oss-crs"

  install -d -o "${CRSBENCH_USER}" -g "${CRSBENCH_USER}" -m 0755 "${CRSBENCH_USER_HOME}"
  install -d -o "${CRSBENCH_USER}" -g "${CRSBENCH_USER}" -m 0755 "${CRSBENCH_USER_HOME}/.config"
  install -d -o "${CRSBENCH_USER}" -g "${CRSBENCH_USER}" -m 0755 "${USER_SERVICE_DIR}"
  install -d -o "${CRSBENCH_USER}" -g "${CRSBENCH_USER}" -m 0755 "${STATE_DIR}"
}

ensure_passwordless_sudo() {
  install -d -m 0755 /etc/sudoers.d
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${CRSBENCH_USER}" > "/etc/sudoers.d/90-${CRSBENCH_USER}"
  chmod 0440 "/etc/sudoers.d/90-${CRSBENCH_USER}"
}

ensure_docker_group_membership() {
  if ! grep -q '^docker:' /etc/group 2>/dev/null; then
    if command -v groupadd >/dev/null 2>&1; then
      groupadd --force docker
    elif command -v addgroup >/dev/null 2>&1; then
      addgroup -S docker >/dev/null 2>&1 || true
    fi
  fi

  if command -v usermod >/dev/null 2>&1; then
    usermod -aG docker "${CRSBENCH_USER}"
  elif command -v addgroup >/dev/null 2>&1; then
    addgroup "${CRSBENCH_USER}" docker >/dev/null 2>&1 || true
  fi
}

ensure_docker_buildx_builder() {
  run_as_crsbench env \
    PATH="${CRSBENCH_USER_PATH}" \
    HOME="${CRSBENCH_USER_HOME}" \
    CRSBENCH_BUILDER_NAME="${CRSBENCH_BUILDX_BUILDER_NAME}" \
    /bin/bash <<'EOF'
set -euo pipefail

inspect_output="$(docker buildx inspect "${CRSBENCH_BUILDER_NAME}" 2>/dev/null || true)"
if [[ -n "${inspect_output}" ]] && ! grep -Eq 'Driver:[[:space:]]+docker-container' <<<"${inspect_output}"; then
  docker buildx rm "${CRSBENCH_BUILDER_NAME}" >/dev/null 2>&1 || true
  inspect_output=""
fi

if [[ -z "${inspect_output}" ]]; then
  docker buildx create --name "${CRSBENCH_BUILDER_NAME}" --driver docker-container --use >/dev/null
else
  docker buildx use "${CRSBENCH_BUILDER_NAME}" >/dev/null
fi

docker buildx inspect --bootstrap "${CRSBENCH_BUILDER_NAME}" >/dev/null
EOF
}

run_as_crsbench() {
  sudo -E -H -u "${CRSBENCH_USER}" "$@"
}

run_crsbench_shell() {
  run_as_crsbench env PATH="${CRSBENCH_USER_PATH}" HOME="${CRSBENCH_USER_HOME}" /bin/bash -lc "$1"
}

prepare_clone_dir() {
  rm -rf "${CLONE_DIR}"
  install -d -o "${CRSBENCH_USER}" -g "${CRSBENCH_USER}" -m 0755 "${CLONE_DIR}"
}

configure_clone_ssh() {
  local deploy_key_b64="$1"
  if [[ -z "${deploy_key_b64}" ]]; then
    return 0
  fi

  install -d -o "${CRSBENCH_USER}" -g "${CRSBENCH_USER}" -m 0700 "${CRSBENCH_USER_HOME}/.ssh"
  printf '%s' "${deploy_key_b64}" | base64 --decode > "${CRSBENCH_USER_HOME}/.ssh/id_ed25519"
  chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${CRSBENCH_USER_HOME}/.ssh/id_ed25519"
  chmod 0600 "${CRSBENCH_USER_HOME}/.ssh/id_ed25519"
  touch "${CRSBENCH_USER_HOME}/.ssh/known_hosts"
  chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${CRSBENCH_USER_HOME}/.ssh/known_hosts"
  chmod 0600 "${CRSBENCH_USER_HOME}/.ssh/known_hosts"
  run_crsbench_shell "ssh-keyscan -t ed25519 github.com >> ${CRSBENCH_USER_HOME}/.ssh/known_hosts 2>/dev/null"
  CLONE_GIT_SSH_COMMAND="ssh -F /dev/null -i ${CRSBENCH_USER_HOME}/.ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=${CRSBENCH_USER_HOME}/.ssh/known_hosts -o StrictHostKeyChecking=yes"
}

clone_repo_as_crsbench() {
  local repo_url="$1"
  local clone_dir="$2"
  if [[ "${repo_url}" == file://* ]]; then
    local repo_path="${repo_url#file://}"
    if [[ "${repo_path}" == localhost/* ]]; then
      repo_path="/${repo_path#localhost/}"
    fi
    if [[ -d "${repo_path}" ]]; then
      run_as_crsbench git config --global --add safe.directory "${repo_path}" || true
      if [[ -d "${repo_path}/.git" ]]; then
        run_as_crsbench git config --global --add safe.directory "${repo_path}/.git" || true
      fi
    fi
  fi

  if [[ -n "${CLONE_GIT_SSH_COMMAND}" ]]; then
    sudo -H -u "${CRSBENCH_USER}" env GIT_SSH_COMMAND="${CLONE_GIT_SSH_COMMAND}" git clone --no-single-branch "${repo_url}" "${clone_dir}"
    return
  fi
  run_as_crsbench git clone --no-single-branch "${repo_url}" "${clone_dir}"
}

ensure_uv_for_crsbench() {
  if run_crsbench_shell 'command -v uv >/dev/null 2>&1'; then
    return 0
  fi
  run_crsbench_shell 'curl -LsSf https://astral.sh/uv/install.sh | sh'
}

enable_cgroup_controllers() {
  local subtree_control_path="$1"
  local current=""
  if [[ -f "${subtree_control_path}" ]]; then
    current="$(cat "${subtree_control_path}" 2>/dev/null || true)"
  fi

  local -a missing=()
  if ! grep -qw cpuset <<<"${current}"; then
    missing+=("+cpuset")
  fi
  if ! grep -qw memory <<<"${current}"; then
    missing+=("+memory")
  fi
  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi
  printf '%s\n' "${missing[*]}" > "${subtree_control_path}"
}

wait_for_user_manager() {
  for _i in $(seq 1 30); do
    if systemctl is-active --quiet "user@${CRSBENCH_USER_UID}.service" \
      && [[ -d "${CRSBENCH_USER_SERVICE_CGROUP}" ]] \
      && [[ -S "${CRSBENCH_USER_RUNTIME_DIR}/bus" ]]; then
      return 0
    fi
    sleep 1
  done

  echo "systemd user manager did not become ready for ${CRSBENCH_USER}" >&2
  exit 1
}

ensure_runtime_cgroup_hierarchy() {
  local cgroup_path="$1"

  mkdir -p "${cgroup_path}"
  chown -R "${CRSBENCH_USER_UID}:${CRSBENCH_USER_GID}" "${cgroup_path}"
  enable_cgroup_controllers "${cgroup_path}/cgroup.subtree_control"
}

ensure_runtime_cgroup_hierarchies() {
  if ! service_manager_uses_systemd; then
    return 0
  fi

  if [[ ! -d "${CRSBENCH_USER_SERVICE_CGROUP}" ]]; then
    echo "Missing user service cgroup for ${CRSBENCH_USER}: ${CRSBENCH_USER_SERVICE_CGROUP}" >&2
    exit 1
  fi

  enable_cgroup_controllers "${CRSBENCH_USER_SERVICE_CGROUP}/cgroup.subtree_control"
  ensure_runtime_cgroup_hierarchy "${CRSBENCH_RUNTIME_CGROUP}"
  ensure_runtime_cgroup_hierarchy "${CRSBENCH_OSS_CRS_CGROUP}"
}

setup_user_systemd_runtime() {
  if ! service_manager_uses_systemd; then
    return 0
  fi
  if ! supports_systemd; then
    echo "CRSBENCH_SERVICE_MANAGER=systemd requires a running systemd host" >&2
    exit 1
  fi
  require_cmd loginctl

  install -d -m 0755 /etc/systemd/system/user@.service.d
  cat > /etc/systemd/system/user@.service.d/delegate.conf <<'EOF'
[Service]
Delegate=cpuset memory
EOF
  systemctl daemon-reload
  loginctl enable-linger "${CRSBENCH_USER}"
  systemctl restart "user@${CRSBENCH_USER_UID}.service" || systemctl start "user@${CRSBENCH_USER_UID}.service"
  wait_for_user_manager
  ensure_runtime_cgroup_hierarchies
}

run_user_systemctl() {
  sudo -H -u "${CRSBENCH_USER}" env \
    XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
    DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
    systemctl --user "$@"
}

setup_oss_crs_for_crsbench() {
  if ! service_manager_uses_systemd; then
    return 0
  fi
  run_crsbench_shell "cd $(printf '%q' "${CLONE_DIR}") && ${CLONE_DIR}/.venv/bin/oss-crs setup --yes"
  run_crsbench_shell "cd $(printf '%q' "${CLONE_DIR}") && ${CLONE_DIR}/.venv/bin/oss-crs setup --check"
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
    detail="GCE ${CRSBENCH_STARTUP_MODE:-worker} bootstrap failed",
    startup_evidence=sys.argv[2],
)
PY
}

on_error() {
  report_bootstrap_failure "startup script failed at line $1: $2"
}

start_worker_runtime() {
  case "${CRSBENCH_SERVICE_MANAGER}" in
    auto)
      if supports_systemd; then
        run_user_systemctl daemon-reload
        if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
          sudo -H -u "${CRSBENCH_USER}" env \
            XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
            DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
            systemctl --user enable --now crsbench-evaluator.service
        else
          sudo -H -u "${CRSBENCH_USER}" env \
            XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
            DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
            systemctl --user enable --now crsbench-worker.service
        fi
        return 0
      fi
      ;;
    systemd)
      if ! supports_systemd; then
        echo "CRSBENCH_SERVICE_MANAGER=systemd requires a running systemd host" >&2
        exit 1
      fi
      run_user_systemctl daemon-reload
      if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
        sudo -H -u "${CRSBENCH_USER}" env \
          XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
          DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
          systemctl --user enable --now crsbench-evaluator.service
      else
        sudo -H -u "${CRSBENCH_USER}" env \
          XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
          DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
          systemctl --user enable --now crsbench-worker.service
      fi
      return 0
      ;;
    foreground)
      ;;
    *)
      echo "Unsupported CRSBENCH_SERVICE_MANAGER: ${CRSBENCH_SERVICE_MANAGER}" >&2
      exit 1
      ;;
  esac

  exec sudo -H -u "${CRSBENCH_USER}" /bin/bash -lc \
    "cd $(printf '%q' "${CLONE_DIR}") && exec $(printf '%q' "${LAUNCHER_PATH}")"
}

require_cmd curl

mkdir -p "${STATE_DIR}"

# --- Install system packages ---
ensure_system_packages
ensure_timezone
ensure_docker_ready
ensure_crsbench_user
ensure_passwordless_sudo
ensure_docker_group_membership
ensure_docker_buildx_builder
ensure_user_systemd_support_packages

metadata_get "crsbench-bootstrap-payload" | base64 --decode > "${PAYLOAD_PATH}"

readarray -t PAYLOAD_FIELDS < <(
  python3 - "${PAYLOAD_PATH}" "${CRSBENCH_STARTUP_MODE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

mode = sys.argv[2]
print(payload["redis_host"])
print(payload["experiment"])
if mode == "evaluator":
    print(payload.get("evaluator_name") or "")
    print(payload.get("evaluator_build_jobs") or "")
    print(payload.get("evaluator_build_cores_per_job") or "")
    print(payload.get("evaluator_verify_jobs") or "")
    print(payload.get("evaluator_verify_cores_per_job") or "")
    print(payload.get("evaluator_idle_timeout") or "")
    print(payload.get("evaluator_cpu_tag") or "")
else:
    print(payload.get("worker_name") or "")
    print(payload.get("worker_jobs") or "")
    print(payload.get("worker_cores_per_job") or "")
    print(payload.get("worker_cpu_tag") or "")
print(payload.get("readiness_timeout_sec") or "")
PY
)

REDIS_HOST="${PAYLOAD_FIELDS[0]}"
EXPERIMENT_NAME="${PAYLOAD_FIELDS[1]}"
READINESS_TIMEOUT_SEC=""
WORKER_NAME=""
WORKER_JOBS=""
WORKER_CORES_PER_JOB=""
WORKER_CPU_TAG=""
EVALUATOR_NAME=""
EVALUATOR_BUILD_JOBS=""
EVALUATOR_BUILD_CORES_PER_JOB=""
EVALUATOR_VERIFY_JOBS=""
EVALUATOR_VERIFY_CORES_PER_JOB=""
EVALUATOR_IDLE_TIMEOUT=""
EVALUATOR_CPU_TAG=""
if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  EVALUATOR_NAME="${PAYLOAD_FIELDS[2]}"
  if [[ -z "${EVALUATOR_NAME}" ]]; then
    EVALUATOR_NAME="$(instance_metadata_get "name")"
  fi
  EVALUATOR_BUILD_JOBS="${PAYLOAD_FIELDS[3]}"
  EVALUATOR_BUILD_CORES_PER_JOB="${PAYLOAD_FIELDS[4]}"
  EVALUATOR_VERIFY_JOBS="${PAYLOAD_FIELDS[5]}"
  EVALUATOR_VERIFY_CORES_PER_JOB="${PAYLOAD_FIELDS[6]}"
  EVALUATOR_IDLE_TIMEOUT="${PAYLOAD_FIELDS[7]}"
  EVALUATOR_CPU_TAG="${PAYLOAD_FIELDS[8]}"
  READINESS_TIMEOUT_SEC="${PAYLOAD_FIELDS[9]}"
else
  WORKER_NAME="${PAYLOAD_FIELDS[2]}"
  if [[ -z "${WORKER_NAME}" ]]; then
    WORKER_NAME="$(instance_metadata_get "name")"
  fi
  WORKER_JOBS="${PAYLOAD_FIELDS[3]}"
  WORKER_CORES_PER_JOB="${PAYLOAD_FIELDS[4]}"
  WORKER_CPU_TAG="${PAYLOAD_FIELDS[5]}"
  READINESS_TIMEOUT_SEC="${PAYLOAD_FIELDS[6]}"
fi
REDIS_PASSWORD="$(metadata_get_optional "crsbench-redis-password")"
INSTANCE_ID="$(instance_metadata_get "id")"
ZONE_PATH="$(instance_metadata_get "zone")"
ZONE="${ZONE_PATH##*/}"
if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  metadata_get_optional "crsbench-experiment-config-b64" | base64 --decode > "${EXPERIMENT_CONFIG_PATH}"
fi

export CRSBENCH_REDIS_HOST="${REDIS_HOST}"
export CRSBENCH_REDIS_PASSWORD="${REDIS_PASSWORD}"
export CRSBENCH_EXPERIMENT_NAME="${EXPERIMENT_NAME}"
export CRSBENCH_READINESS_TIMEOUT_SEC="${READINESS_TIMEOUT_SEC}"
export CRSBENCH_CLOUD_EXPERIMENT="${EXPERIMENT_NAME}"
export CRSBENCH_CLOUD_INSTANCE_ID="${INSTANCE_ID}"
export CRSBENCH_CLOUD_ROLE="${CRSBENCH_STARTUP_MODE}"
export CRSBENCH_CLOUD_ZONE="${ZONE}"
if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  export CRSBENCH_EVALUATOR_NAME="${EVALUATOR_NAME}"
  export CRSBENCH_EVALUATOR_BUILD_JOBS="${EVALUATOR_BUILD_JOBS}"
  export CRSBENCH_EVALUATOR_BUILD_CORES_PER_JOB="${EVALUATOR_BUILD_CORES_PER_JOB}"
  export CRSBENCH_EVALUATOR_VERIFY_JOBS="${EVALUATOR_VERIFY_JOBS}"
  export CRSBENCH_EVALUATOR_VERIFY_CORES_PER_JOB="${EVALUATOR_VERIFY_CORES_PER_JOB}"
  export CRSBENCH_EVALUATOR_IDLE_TIMEOUT="${EVALUATOR_IDLE_TIMEOUT}"
  export CRSBENCH_EVALUATOR_CPU_TAG="${EVALUATOR_CPU_TAG}"
  export CRSBENCH_EXPERIMENT_CONFIG_PATH="${EXPERIMENT_CONFIG_PATH}"
  export CRSBENCH_CLOUD_INSTANCE_NAME="${EVALUATOR_NAME}"
else
  export CRSBENCH_WORKER_NAME="${WORKER_NAME}"
  export CRSBENCH_WORKER_JOBS="${WORKER_JOBS}"
  export CRSBENCH_WORKER_CORES_PER_JOB="${WORKER_CORES_PER_JOB}"
  export CRSBENCH_WORKER_CPU_TAG="${WORKER_CPU_TAG}"
  export CRSBENCH_CLOUD_INSTANCE_NAME="${WORKER_NAME}"
fi

trap 'on_error "${LINENO}" "${BASH_COMMAND}"' ERR

INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
GIT_REF="$(metadata_get_optional "crsbench-git-ref")"
GITHUB_DEPLOY_KEY="$(metadata_get_optional "crsbench-github-deploy-key")"
ENV_PASSTHROUGH_B64="$(metadata_get_optional "crsbench-env-passthrough-b64")"

# --- GitHub SSH setup (if deploy key provided) ---
configure_clone_ssh "${GITHUB_DEPLOY_KEY}"
export_passthrough_env "${ENV_PASSTHROUGH_B64}"

# --- Install crsbench from a repo checkout ---
if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then
  echo "cloud worker bootstrap requires git+ install spec metadata (role=${CRSBENCH_STARTUP_MODE:-worker})" >&2
  exit 1
fi
REPO_URL="${INSTALL_SPEC#git+}"
prepare_clone_dir
clone_repo_as_crsbench "${REPO_URL}" "${CLONE_DIR}"
run_crsbench_shell "cd $(printf '%q' "${CLONE_DIR}") && git checkout $(printf '%q' "${GIT_REF:-main}") && git submodule update --init --recursive"
ensure_uv_for_crsbench
run_crsbench_shell "cd $(printf '%q' "${CLONE_DIR}") && uv sync --all-extras && uv pip install -e ."
VENV_BIN="${CLONE_DIR}/.venv/bin"
CRSBENCH_USER_PATH="${VENV_BIN}:${CRSBENCH_USER_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

run_as_crsbench env PATH="${CRSBENCH_USER_PATH}" HOME="${CRSBENCH_USER_HOME}" /bin/bash -lc "cd $(printf '%q' "${CLONE_DIR}") && python3 - $(printf '%q' "${PAYLOAD_PATH}") <<'PY'
import json
import sys
from pathlib import Path

from crsbench.cloud.bootstrap import bootstrap_inputs_from_payload, run_cloud_vm_bootstrap

payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
run_cloud_vm_bootstrap(
    bootstrap_inputs_from_payload(payload),
    cwd=Path.cwd(),
)
PY"

setup_user_systemd_runtime
setup_oss_crs_for_crsbench

: > "${ENV_PATH}"
write_env_var "CRSBENCH_REDIS_HOST" "${REDIS_HOST}"
if [[ -n "${REDIS_PASSWORD}" ]]; then
  write_env_var "CRSBENCH_REDIS_PASSWORD" "${REDIS_PASSWORD}"
fi
write_env_var "CRSBENCH_EXPERIMENT_NAME" "${EXPERIMENT_NAME}"
write_env_var "CRSBENCH_READINESS_TIMEOUT_SEC" "${READINESS_TIMEOUT_SEC}"
write_env_var "CRSBENCH_CLOUD_EXPERIMENT" "${EXPERIMENT_NAME}"
write_env_var "CRSBENCH_CLOUD_INSTANCE_ID" "${INSTANCE_ID}"
write_env_var "CRSBENCH_CLOUD_ROLE" "${CRSBENCH_STARTUP_MODE}"
write_env_var "CRSBENCH_CLOUD_ZONE" "${ZONE}"
write_env_var "CRSBENCH_LOG_LEVEL" "INFO"
if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  write_env_var "CRSBENCH_EVALUATOR_NAME" "${EVALUATOR_NAME}"
  write_env_var "CRSBENCH_EVALUATOR_BUILD_JOBS" "${EVALUATOR_BUILD_JOBS}"
  write_env_var "CRSBENCH_EVALUATOR_BUILD_CORES_PER_JOB" "${EVALUATOR_BUILD_CORES_PER_JOB}"
  write_env_var "CRSBENCH_EVALUATOR_VERIFY_JOBS" "${EVALUATOR_VERIFY_JOBS}"
  write_env_var "CRSBENCH_EVALUATOR_VERIFY_CORES_PER_JOB" "${EVALUATOR_VERIFY_CORES_PER_JOB}"
  write_env_var "CRSBENCH_EVALUATOR_IDLE_TIMEOUT" "${EVALUATOR_IDLE_TIMEOUT}"
  write_env_var "CRSBENCH_EVALUATOR_CPU_TAG" "${EVALUATOR_CPU_TAG}"
  write_env_var "CRSBENCH_EXPERIMENT_CONFIG_PATH" "${EXPERIMENT_CONFIG_PATH}"
  write_env_var "CRSBENCH_CLOUD_INSTANCE_NAME" "${EVALUATOR_NAME}"
else
  write_env_var "CRSBENCH_WORKER_NAME" "${WORKER_NAME}"
  write_env_var "CRSBENCH_WORKER_JOBS" "${WORKER_JOBS}"
  write_env_var "CRSBENCH_WORKER_CORES_PER_JOB" "${WORKER_CORES_PER_JOB}"
  write_env_var "CRSBENCH_WORKER_CPU_TAG" "${WORKER_CPU_TAG}"
  write_env_var "CRSBENCH_CLOUD_INSTANCE_NAME" "${WORKER_NAME}"
fi
write_passthrough_env_vars "${ENV_PASSTHROUGH_B64}"
if [[ -n "${VENV_BIN:-}" ]]; then
  write_env_var "PATH" "${VENV_BIN}:${CRSBENCH_USER_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
fi

cat > "${LAUNCHER_PATH}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

CRSBENCH_ENV_PATH="__CRSBENCH_ENV_PATH__"

# shellcheck disable=SC1090
set -a
source "${CRSBENCH_ENV_PATH}"
set +a

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
    detail="GCE ${CRSBENCH_CLOUD_ROLE:-worker} service failed",
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

cmd=(/usr/bin/env crsbench)
if [[ "${CRSBENCH_CLOUD_ROLE:-worker}" == "evaluator" ]]; then
  # Launch crsbench evaluator in config-pinned mode on managed evaluator VMs.
  cmd+=(
    evaluator
    --experiment-config
    "${CRSBENCH_EXPERIMENT_CONFIG_PATH}"
    --worker-name
    "${CRSBENCH_EVALUATOR_NAME}"
  )

  if [[ -n "${CRSBENCH_EVALUATOR_BUILD_JOBS:-}" ]]; then
    cmd+=(--build-jobs "${CRSBENCH_EVALUATOR_BUILD_JOBS}")
  fi

  if [[ -n "${CRSBENCH_EVALUATOR_BUILD_CORES_PER_JOB:-}" ]]; then
    cmd+=(--build-cores-per-job "${CRSBENCH_EVALUATOR_BUILD_CORES_PER_JOB}")
  fi

  if [[ -n "${CRSBENCH_EVALUATOR_VERIFY_JOBS:-}" ]]; then
    cmd+=(--verify-jobs "${CRSBENCH_EVALUATOR_VERIFY_JOBS}")
  fi

  if [[ -n "${CRSBENCH_EVALUATOR_VERIFY_CORES_PER_JOB:-}" ]]; then
    cmd+=(--verify-cores-per-job "${CRSBENCH_EVALUATOR_VERIFY_CORES_PER_JOB}")
  fi

  if [[ -n "${CRSBENCH_EVALUATOR_IDLE_TIMEOUT:-}" ]]; then
    cmd+=(--idle-timeout "${CRSBENCH_EVALUATOR_IDLE_TIMEOUT}")
  fi

  if [[ -n "${CRSBENCH_EVALUATOR_CPU_TAG:-}" ]]; then
    cmd+=(--cpu-tag "${CRSBENCH_EVALUATOR_CPU_TAG}")
  fi
else
  cmd+=(
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
fi

wait_for_redis

set +e
"${cmd[@]}"
exit_code="$?"
set -e
if [[ "${exit_code}" -eq 0 ]]; then
  exit 0
fi

report_bootstrap_failure "${CRSBENCH_CLOUD_ROLE:-worker} service exited with status ${exit_code}"
exit "${exit_code}"
EOF
python3 - "${LAUNCHER_PATH}" "${ENV_PATH}" <<'PY'
from pathlib import Path
import sys

launcher_path = Path(sys.argv[1])
env_path = sys.argv[2]
launcher_path.write_text(
    launcher_path.read_text(encoding="utf-8").replace("__CRSBENCH_ENV_PATH__", env_path),
    encoding="utf-8",
)
PY
chmod +x "${LAUNCHER_PATH}"
if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${PAYLOAD_PATH}" "${ENV_PATH}" "${LAUNCHER_PATH}" "${EXPERIMENT_CONFIG_PATH}"
else
  chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${PAYLOAD_PATH}" "${ENV_PATH}" "${LAUNCHER_PATH}"
fi

SERVICE_DESCRIPTION="CRSBench worker service"
if [[ "${CRSBENCH_STARTUP_MODE}" == "evaluator" ]]; then
  SERVICE_DESCRIPTION="CRSBench evaluator service"
fi

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=${SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_PATH}
WorkingDirectory=${CLONE_DIR}
ExecStart=/bin/bash ${LAUNCHER_PATH}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${SERVICE_PATH}"

start_worker_runtime

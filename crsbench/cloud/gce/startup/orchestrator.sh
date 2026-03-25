#!/usr/bin/env bash
# Startup script for a GCE orchestrator VM.
#
# Expected instance metadata:
#   crsbench-install-spec       git+https://..., git+ssh://..., or pip spec (required)
#   crsbench-experiment-config-b64  base64-encoded experiment YAML payload (required)
#   crsbench-redis-password     shared Valkey password (required)
#   crsbench-git-ref            branch/tag to clone (default: main)
#   crsbench-github-deploy-key  base64-encoded SSH private key (optional)
#   crsbench-env-passthrough-b64  base64-encoded resolved remote env map (optional)
#
# The script:
#   1. Clones CRSBench as the crsbench user
#   2. Prepares/downloads shared cloud assets from that checkout
#   3. Configures Docker cgroupfs and user-systemd delegation for oss-crs on real VMs
#   4. Starts a managed crsbench user service or foreground launcher
set -euo pipefail

CRSBENCH_METADATA_ROOT_DIR="${CRSBENCH_METADATA_ROOT_DIR:-}"
CRSBENCH_METADATA_BASE_URL="${CRSBENCH_METADATA_BASE_URL:-http://metadata.google.internal/computeMetadata/v1}"
CRSBENCH_METADATA_HEADER_NAME="${CRSBENCH_METADATA_HEADER_NAME:-Metadata-Flavor}"
CRSBENCH_METADATA_HEADER_VALUE="${CRSBENCH_METADATA_HEADER_VALUE:-Google}"
CRSBENCH_SERVICE_MANAGER="${CRSBENCH_SERVICE_MANAGER:-auto}"
CRSBENCH_TIMEZONE="${CRSBENCH_TIMEZONE:-America/New_York}"
CRSBENCH_GIT_SSH_HOST="${CRSBENCH_GIT_SSH_HOST:-github.com}"
CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"
CRSBENCH_USER_HOME="${CRSBENCH_USER_HOME:-/home/${CRSBENCH_USER}}"
CRSBENCH_MANAGED_BIN_DIR="${CRSBENCH_MANAGED_BIN_DIR:-/opt/crsbench-managed/bin}"
CRSBENCH_GITCACHE_ENABLED="${CRSBENCH_GITCACHE_ENABLED:-0}"
CRSBENCH_REDIS_BIND_HOST="${CRSBENCH_REDIS_BIND_HOST:-}"
CRSBENCH_VALKEY_IMAGE="${CRSBENCH_VALKEY_IMAGE:-valkey/valkey:8.0-alpine}"
STATE_DIR="${CRSBENCH_STATE_DIR:-/var/lib/crsbench}"
LOG_PATH="${STATE_DIR}/orchestrator.log"
CONFIG_PATH="${STATE_DIR}/experiment-config.yaml"
LAUNCHER_PATH="${STATE_DIR}/launch-orchestrator.sh"
ENV_PATH="${STATE_DIR}/orchestrator.env"
CLONE_DIR="${CRSBENCH_CLONE_DIR:-/opt/crsbench}"
DOCKER_DAEMON_CONFIG_PATH="${CRSBENCH_DOCKER_DAEMON_CONFIG_PATH:-/etc/docker/daemon.json}"
DOCKER_CGROUP_DRIVER_OPT="${CRSBENCH_DOCKER_CGROUP_DRIVER_OPT:-native.cgroupdriver=cgroupfs}"
USER_SERVICE_DIR="${CRSBENCH_USER_HOME}/.config/systemd/user"
SERVICE_PATH="${USER_SERVICE_DIR}/crsbench-orchestrator.service"
CLONE_GIT_SSH_COMMAND=""
CRSBENCH_USER_UID=""
CRSBENCH_USER_GID=""
CRSBENCH_USER_RUNTIME_DIR=""
CRSBENCH_USER_DBUS_ADDRESS=""
CRSBENCH_USER_LOCAL_BIN=""
CRSBENCH_USER_PATH=""
CRSBENCH_USER_SERVICE_CGROUP=""
CRSBENCH_OSS_CRS_CGROUP=""

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

discover_redis_bind_host() {
  if [[ -n "${CRSBENCH_REDIS_BIND_HOST}" ]]; then
    printf '%s\n' "${CRSBENCH_REDIS_BIND_HOST}"
    return 0
  fi

  local metadata_ip=""
  metadata_ip="$(instance_metadata_get "network-interfaces/0/ip" 2>/dev/null || true)"
  if [[ -n "${metadata_ip}" ]]; then
    printf '%s\n' "${metadata_ip}"
    return 0
  fi

  python3 <<'PY'
import socket


def is_usable_ipv4(value: str) -> bool:
    return bool(value) and "." in value and not value.startswith("127.") and value != "0.0.0.0"


def emit(value: str) -> None:
    if is_usable_ipv4(value):
        print(value)
        raise SystemExit(0)


probe = None
try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("8.8.8.8", 80))
    emit(probe.getsockname()[0])
except OSError:
    pass
finally:
    if probe is not None:
        probe.close()


def candidate_ips() -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    for hostname in (socket.gethostname(), socket.getfqdn()):
        try:
            _, _, addresses = socket.gethostbyname_ex(hostname)
        except OSError:
            continue
        for address in addresses:
            add(address)

    for value in candidates:
        if is_usable_ipv4(value):
            return [value]
    return []


matches = candidate_ips()
if matches:
    print(matches[0])
    raise SystemExit(0)

raise SystemExit(
    "Unable to determine non-loopback IPv4 address for Valkey bind host"
)
PY
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

write_env_var() {
  printf "%s=%q\n" "$1" "$2" >> "${ENV_PATH}"
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
    && command -v iftop >/dev/null 2>&1 \
    && command -v rg >/dev/null 2>&1 \
    && [[ "${have_fd_find}" -eq 0 ]] \
    && command -v ssh-keyscan >/dev/null 2>&1 \
    && command -v sudo >/dev/null 2>&1 \
    && [[ -e /usr/share/zoneinfo/UTC ]]; then
    return 0
  fi

  echo "Installing system packages..."
  if command -v apt-get >/dev/null 2>&1; then
    install_packages git python3 python3-pip python3-venv python3-yaml rsync tar bash coreutils openssh-client tzdata sudo iftop ripgrep fd-find
    return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    install_packages git python3 py3-pip py3-yaml rsync tar bash coreutils openssh-client tzdata sudo shadow iftop ripgrep fd
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

ensure_docker_official_apt_repo() {
  install_packages ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  local arch codename
  arch="$(dpkg --print-architecture)"
  codename="$(
    . /etc/os-release
    printf '%s' "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  )"
  if [[ -z "${codename}" ]]; then
    echo "Unable to determine Ubuntu codename for Docker apt repository" >&2
    exit 1
  fi

  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
    "${arch}" "${codename}" > /etc/apt/sources.list.d/docker.list
}

remove_conflicting_docker_apt_packages() {
  DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq \
    docker.io docker-doc docker-compose podman-docker containerd runc >/dev/null 2>&1 || true
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
      remove_conflicting_docker_apt_packages
      ensure_docker_official_apt_repo
      apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
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
  SERVICE_PATH="${USER_SERVICE_DIR}/crsbench-orchestrator.service"
  CRSBENCH_USER_UID="$(id -u "${CRSBENCH_USER}")"
  CRSBENCH_USER_GID="$(id -g "${CRSBENCH_USER}")"
  CRSBENCH_USER_RUNTIME_DIR="/run/user/${CRSBENCH_USER_UID}"
  CRSBENCH_USER_DBUS_ADDRESS="unix:path=${CRSBENCH_USER_RUNTIME_DIR}/bus"
  CRSBENCH_USER_LOCAL_BIN="${CRSBENCH_USER_HOME}/.local/bin"
  CRSBENCH_USER_PATH="${CRSBENCH_MANAGED_BIN_DIR}:${CRSBENCH_USER_LOCAL_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  CRSBENCH_USER_SERVICE_CGROUP="/sys/fs/cgroup/user.slice/user-${CRSBENCH_USER_UID}.slice/user@${CRSBENCH_USER_UID}.service"
  CRSBENCH_RUNTIME_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/crsbench"
  CRSBENCH_OSS_CRS_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/oss-crs"

  install -d -m 0755 "${CRSBENCH_MANAGED_BIN_DIR}"
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
    /bin/bash <<'EOF'
set -euo pipefail

docker buildx inspect --bootstrap >/dev/null
EOF
}

run_as_crsbench() {
  sudo -H -u "${CRSBENCH_USER}" "$@"
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
  run_crsbench_shell "ssh-keyscan -t ed25519 \"${CRSBENCH_GIT_SSH_HOST}\" >> ${CRSBENCH_USER_HOME}/.ssh/known_hosts 2>/dev/null"
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
    sudo -H -u "${CRSBENCH_USER}" env PATH="${CRSBENCH_USER_PATH}" HOME="${CRSBENCH_USER_HOME}" GIT_SSH_COMMAND="${CLONE_GIT_SSH_COMMAND}" git clone --no-single-branch "${repo_url}" "${clone_dir}"
    return
  fi
  run_as_crsbench env PATH="${CRSBENCH_USER_PATH}" HOME="${CRSBENCH_USER_HOME}" git clone --no-single-branch "${repo_url}" "${clone_dir}"
}

read_gitcache_flag_from_config() {
  python3 - "$1" <<'PY'
import sys

import yaml
from pathlib import Path

config_path = Path(sys.argv[1])
raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
if not isinstance(raw, dict):
    print("0")
    raise SystemExit(0)

cloud = raw.get("cloud")
if not isinstance(cloud, dict):
    print("0")
    raise SystemExit(0)

bootstrap = cloud.get("bootstrap")
if not isinstance(bootstrap, dict):
    print("0")
    raise SystemExit(0)

print("1" if bool(bootstrap.get("gitcache")) else "0")
PY
}

gitcache_release_asset_name() {
  local arch
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64)
      arch="x86_64"
      ;;
    aarch64|arm64)
      arch="aarch64"
      ;;
    *)
      echo "Unsupported gitcache architecture: ${arch}" >&2
      return 1
      ;;
  esac

  local os_id=""
  local version_id=""
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    os_id="${ID:-}"
    version_id="${VERSION_ID:-}"
  fi

  case "${os_id}:${version_id}:${arch}" in
    ubuntu:24.04:x86_64)
      printf '%s\n' "gitcache_v1.0.31_Ubuntu24.04_x86_64"
      ;;
    ubuntu:24.04:aarch64)
      printf '%s\n' "gitcache_v1.0.31_Ubuntu24.04_aarch64"
      ;;
    ubuntu:22.04:x86_64)
      printf '%s\n' "gitcache_v1.0.31_Ubuntu22.04_x86_64"
      ;;
    ubuntu:22.04:aarch64)
      printf '%s\n' "gitcache_v1.0.31_Ubuntu22.04_aarch64"
      ;;
    alpine:3.20:x86_64)
      printf '%s\n' "gitcache_v1.0.31_Alpine3.20_x86_64"
      ;;
    *)
      echo "Unsupported gitcache platform: ${os_id:-unknown} ${version_id:-unknown} ${arch}" >&2
      return 1
      ;;
  esac
}

install_gitcache_binary() {
  local asset_name release_url tmp_path
  asset_name="$(gitcache_release_asset_name)"
  release_url="https://github.com/seeraven/gitcache/releases/download/v1.0.31/${asset_name}"
  tmp_path="$(mktemp)"
  if ! curl -fsSL "${release_url}" -o "${tmp_path}"; then
    rm -f "${tmp_path}"
    return 1
  fi
  install -d -m 0755 "${CRSBENCH_MANAGED_BIN_DIR}"
  install -m 0755 "${tmp_path}" "${CRSBENCH_MANAGED_BIN_DIR}/gitcache"
  rm -f "${tmp_path}"
}

enable_gitcache_wrapper() {
  install -d -m 0755 "${CRSBENCH_MANAGED_BIN_DIR}"
  ln -sfn "${CRSBENCH_MANAGED_BIN_DIR}/gitcache" "${CRSBENCH_MANAGED_BIN_DIR}/git"
}

disable_gitcache_wrapper() {
  rm -f "${CRSBENCH_MANAGED_BIN_DIR}/git"
}

ensure_gitcache_ready() {
  if ! install_gitcache_binary; then
    echo "gitcache install failed; continuing with system git" >&2
    disable_gitcache_wrapper
    if [[ "${CRSBENCH_GITCACHE_ENABLED}" == "1" ]]; then
      return 1
    fi
    return 0
  fi

  if [[ "${CRSBENCH_GITCACHE_ENABLED}" == "1" ]]; then
    enable_gitcache_wrapper
    return 0
  fi

  disable_gitcache_wrapper
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

start_orchestrator_runtime() {
  case "${CRSBENCH_SERVICE_MANAGER}" in
    auto)
      if supports_systemd; then
        run_user_systemctl daemon-reload
        sudo -H -u "${CRSBENCH_USER}" env \
          XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
          DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
          systemctl --user enable --now crsbench-orchestrator.service
        return 0
      fi
      ;;
    systemd)
      if ! supports_systemd; then
        echo "CRSBENCH_SERVICE_MANAGER=systemd requires a running systemd host" >&2
        exit 1
      fi
      run_user_systemctl daemon-reload
      sudo -H -u "${CRSBENCH_USER}" env \
        XDG_RUNTIME_DIR="${CRSBENCH_USER_RUNTIME_DIR}" \
        DBUS_SESSION_BUS_ADDRESS="${CRSBENCH_USER_DBUS_ADDRESS}" \
        systemctl --user enable --now crsbench-orchestrator.service
      return 0
      ;;
    foreground)
      ;;
    *)
      echo "Unsupported CRSBENCH_SERVICE_MANAGER: ${CRSBENCH_SERVICE_MANAGER}" >&2
      exit 1
      ;;
  esac

  exec sudo -H -u "${CRSBENCH_USER}" /bin/bash "${LAUNCHER_PATH}"
}

require_cmd curl

mkdir -p "${STATE_DIR}"

# --- Install system packages ---
ensure_system_packages
ENV_PASSTHROUGH_B64="$(metadata_get_optional "crsbench-env-passthrough-b64")"
export_passthrough_env "${ENV_PASSTHROUGH_B64}"
ensure_timezone
ensure_docker_ready
ensure_crsbench_user
ensure_passwordless_sudo
ensure_docker_group_membership
ensure_docker_buildx_builder
ensure_user_systemd_support_packages

# --- Read metadata ---
INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
GIT_REF="$(metadata_get_optional "crsbench-git-ref")"
EXPERIMENT_CONFIG_B64="$(metadata_get "crsbench-experiment-config-b64")"
REDIS_PASSWORD="$(metadata_get "crsbench-redis-password")"
GITHUB_DEPLOY_KEY="$(metadata_get_optional "crsbench-github-deploy-key")"
REDIS_BIND_HOST="$(discover_redis_bind_host)"

printf '%s' "${EXPERIMENT_CONFIG_B64}" | base64 --decode > "${CONFIG_PATH}"
CRSBENCH_GITCACHE_ENABLED="$(read_gitcache_flag_from_config "${CONFIG_PATH}")"
ensure_gitcache_ready

# --- GitHub SSH setup (if deploy key provided) ---
configure_clone_ssh "${GITHUB_DEPLOY_KEY}"

# --- Install crsbench from a repo checkout ---
if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then
  echo "cloud orchestrator bootstrap requires git+ install spec metadata" >&2
  exit 1
fi
REPO_URL="${INSTALL_SPEC#git+}"
prepare_clone_dir
clone_repo_as_crsbench "${REPO_URL}" "${CLONE_DIR}"
run_crsbench_shell "cd $(printf '%q' "${CLONE_DIR}") && git checkout $(printf '%q' "${GIT_REF:-main}") && git submodule update --init --recursive"
ensure_uv_for_crsbench
run_crsbench_shell "cd $(printf '%q' "${CLONE_DIR}") && uv sync --all-extras && uv pip install -e ."
VENV_BIN="${CLONE_DIR}/.venv/bin"
CRSBENCH_USER_PATH="${VENV_BIN}:${CRSBENCH_MANAGED_BIN_DIR}:${CRSBENCH_USER_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# --- Decode experiment config and patch redis_host ---
chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${CONFIG_PATH}"

run_as_crsbench env PATH="${CRSBENCH_USER_PATH}" HOME="${CRSBENCH_USER_HOME}" /bin/bash -lc "cd $(printf '%q' "${CLONE_DIR}") && python3 - $(printf '%q' "${CONFIG_PATH}") <<'PY'
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
        raw_config = yaml.safe_load(path.read_text(encoding='utf-8'))
        if not isinstance(raw_config, dict):
            raise ValueError(
                f'Experiment config at {path} must deserialize to a mapping, got '
                f'{type(raw_config).__name__}'
            )

        raw_config['redis_host'] = redis_host

        runtime = raw_config.get('runtime')
        if runtime is None:
            runtime = {}
            raw_config['runtime'] = runtime
        if not isinstance(runtime, dict):
            raise ValueError(\"Experiment config 'runtime' section must be a mapping\")

        runtime_redis = runtime.get('redis')
        if runtime_redis is None:
            runtime_redis = {}
            runtime['redis'] = runtime_redis
        if not isinstance(runtime_redis, dict):
            raise ValueError(
                \"Experiment config 'runtime.redis' section must be a mapping\"
            )

        runtime_redis['host'] = redis_host
        path.write_text(
            yaml.safe_dump(raw_config, sort_keys=False),
            encoding='utf-8',
        )

patch_experiment_config_for_local_redis(sys.argv[1], redis_host='localhost:6379')
PY"

run_as_crsbench env PATH="${CRSBENCH_USER_PATH}" HOME="${CRSBENCH_USER_HOME}" /bin/bash -lc "cd $(printf '%q' "${CLONE_DIR}") && python3 - $(printf '%q' "${CONFIG_PATH}") <<'PY'
import sys
from pathlib import Path

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs, run_cloud_vm_bootstrap
from crsbench.run_experiment import load_experiment_config

config = load_experiment_config(Path(sys.argv[1]))
run_cloud_vm_bootstrap(
    CloudVmBootstrapInputs.from_experiment_config(config),
    cwd=Path.cwd(),
)
PY"

setup_user_systemd_runtime
setup_oss_crs_for_crsbench

: > "${ENV_PATH}"
write_env_var "CRSBENCH_REDIS_PASSWORD" "${REDIS_PASSWORD}"
write_env_var "CRSBENCH_REDIS_BIND_HOST" "${REDIS_BIND_HOST}"
write_env_var "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS" "1"
write_env_var "CONFIG_PATH" "${CONFIG_PATH}"
write_env_var "LOG_PATH" "${LOG_PATH}"
write_passthrough_env_vars "${ENV_PASSTHROUGH_B64}"
if [[ -n "${VENV_BIN:-}" ]]; then
  write_env_var "PATH" "${VENV_BIN}:${CRSBENCH_MANAGED_BIN_DIR}:${CRSBENCH_USER_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
fi

cat > "${LAUNCHER_PATH}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1090
set -a
source "${ENV_PATH}"
set +a

exec > >(tee -a "${LOG_PATH}") 2>&1

echo "=== CRSBench orchestrator bootstrap started at \$(date -u) ==="

ensure_valkey_running() {
  local container_name="crsbench-valkey"
  local volume_name="valkey_valkey-data"
  local -a publish_args=(
    -p "127.0.0.1:6379:6379"
  )

  if [[ -n "\${CRSBENCH_REDIS_BIND_HOST:-}" && "\${CRSBENCH_REDIS_BIND_HOST}" != "127.0.0.1" ]]; then
    publish_args+=(-p "\${CRSBENCH_REDIS_BIND_HOST}:6379:6379")
  fi

  if docker inspect "\${container_name}" >/dev/null 2>&1; then
    if docker inspect --format '{{.State.Running}}' "\${container_name}" 2>/dev/null | grep -q true; then
      return 0
    fi
    docker rm -f "\${container_name}" >/dev/null 2>&1 || true
  fi

  echo "Starting Valkey..."
  docker run -d \
    --name "\${container_name}" \
    "\${publish_args[@]}" \
    -v "\${volume_name}:/data" \
    --restart unless-stopped \
    "${CRSBENCH_VALKEY_IMAGE}" \
    valkey-server \
    --appendonly yes \
    --requirepass "\${CRSBENCH_REDIS_PASSWORD}"

  for _i in \$(seq 1 30); do
    if docker exec -e "REDISCLI_AUTH=\${CRSBENCH_REDIS_PASSWORD}" "\${container_name}" \
         valkey-cli ping 2>/dev/null | grep -q PONG; then
      echo "Valkey is ready"
      return 0
    fi
    sleep 1
  done

  echo "Valkey did not become ready in time" >&2
  return 1
}

ensure_valkey_running

echo "=== Starting crsbench run at \$(date -u) ==="
echo "Config: \${CONFIG_PATH}"

cd "${CLONE_DIR}"
crsbench run --experiment-config "\${CONFIG_PATH}"
EOF
chmod +x "${LAUNCHER_PATH}"
chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${ENV_PATH}" "${CONFIG_PATH}" "${LAUNCHER_PATH}"
touch "${LOG_PATH}"
chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${LOG_PATH}"

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=CRSBench orchestrator service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_PATH}
WorkingDirectory=${CLONE_DIR}
ExecStart=/bin/bash ${LAUNCHER_PATH}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${SERVICE_PATH}"

start_orchestrator_runtime

#!/usr/bin/env bash
set -euo pipefail

METADATA_BASE="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
METADATA_HEADER="Metadata-Flavor: Google"
STATE_DIR="/var/lib/crsbench"
PAYLOAD_PATH="${STATE_DIR}/bootstrap.json"
ENV_PATH="/etc/default/crsbench-worker"
SERVICE_PATH="/etc/systemd/system/crsbench-worker.service"

metadata_get() {
  curl -fsS -H "${METADATA_HEADER}" "${METADATA_BASE}/$1"
}

metadata_get_optional() {
  curl -fsS -H "${METADATA_HEADER}" "${METADATA_BASE}/$1" 2>/dev/null || true
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

require_cmd curl
require_cmd python3
require_cmd systemctl

mkdir -p "${STATE_DIR}"
metadata_get "crsbench-bootstrap-payload" | base64 --decode > "${PAYLOAD_PATH}"

REDIS_HOST="$(
python3 - "${PAYLOAD_PATH}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

print(payload["redis_host"])
PY
)"

WORKER_NAME="$(
python3 - "${PAYLOAD_PATH}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

print(payload["worker_name"])
PY
)"

INSTALL_SPEC="$(metadata_get_optional "crsbench-install-spec")"
if ! command -v crsbench >/dev/null 2>&1; then
  if [[ -n "${INSTALL_SPEC}" ]]; then
    python3 -m pip install --upgrade "${INSTALL_SPEC}"
  else
    echo "crsbench CLI not found and no crsbench-install-spec metadata provided" >&2
    exit 1
  fi
fi

cat > "${ENV_PATH}" <<EOF
CRSBENCH_REDIS_HOST=${REDIS_HOST}
CRSBENCH_WORKER_NAME=${WORKER_NAME}
CRSBENCH_LOG_LEVEL=INFO
EOF

cat > "${SERVICE_PATH}" <<'EOF'
[Unit]
Description=CRSBench worker service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/default/crsbench-worker
ExecStart=/bin/bash -lc '/usr/bin/env crsbench worker --worker-name "$CRSBENCH_WORKER_NAME"'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now crsbench-worker.service

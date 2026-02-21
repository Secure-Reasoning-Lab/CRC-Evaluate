#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run_local_test.sh" --config test-experiment-config-patch-vincent.yaml --gitcache "$@"

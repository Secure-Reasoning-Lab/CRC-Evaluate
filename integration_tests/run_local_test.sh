#!/bin/bash
# Generic local integration test runner.
#
# Usage:
#   ./run_local_test.sh [--config <yaml>] [--gitcache] [--test-dir <dir>] [--skip-sync]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_FILE="$SCRIPT_DIR/test-experiment-config.yaml"
TEST_DIR=""
USE_GITCACHE=false
VERBOSE=true
SKIP_SYNC=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --test-dir)
            TEST_DIR="$2"
            shift 2
            ;;
        --gitcache)
            USE_GITCACHE=true
            shift
            ;;
        --no-verbose)
            VERBOSE=false
            shift
            ;;
        --skip-sync)
            SKIP_SYNC=true
            shift
            ;;
        -h|--help)
            sed -n '2,8p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [[ "$CONFIG_FILE" != /* ]]; then
    CONFIG_FILE="$SCRIPT_DIR/$CONFIG_FILE"
fi

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Config file not found: $CONFIG_FILE"
    exit 1
fi

if [ -z "$TEST_DIR" ]; then
    CONFIG_BASENAME="$(basename "$CONFIG_FILE" .yaml)"
    TEST_DIR="/tmp/crsbench-${CONFIG_BASENAME}/"
fi

echo "=== CRSBench Local Integration Test ==="
echo "Config:   $CONFIG_FILE"
echo "Test dir: $TEST_DIR"
echo ""

cd "$PROJECT_ROOT"
if [ "$SKIP_SYNC" = false ]; then
    echo "Syncing dependencies with uv..."
    uv sync
fi

if [ -d "$TEST_DIR" ]; then
    rm -rf "$TEST_DIR"
fi

CMD=(uv run crsbench run --experiment-config "$CONFIG_FILE")
if [ "$USE_GITCACHE" = true ]; then
    CMD+=(--gitcache)
fi
if [ "$VERBOSE" = true ]; then
    CMD+=(--verbose)
fi

echo "Running: ${CMD[*]}"
echo ""
"${CMD[@]}"

echo ""
echo "=== Integration test completed ==="
echo "Experiment data: ${TEST_DIR}experiment-data"
echo "Reports:         ${TEST_DIR}report-data"
if command -v tree >/dev/null 2>&1 && [ -d "$TEST_DIR" ]; then
    tree -L 4 "$TEST_DIR"
fi

#!/bin/bash
# Integration test script for local execution (no distributed execution)
#
# This script runs a quick integration test of CRSBench using:
# - Local execution mode (no Valkey/Redis)
# - Mock CRS
# - atlanta-nasm-delta-01 benchmark
# - Minimal time limits for fast testing

set -e  # Exit on error

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
CONFIG_FILE="$SCRIPT_DIR/test-experiment-config.yaml"
EXPERIMENT_NAME="integration-test-local"

# Path overrides (CLI arguments have highest precedence)
OSS_FUZZ_PATH="${OSS_FUZZ_PATH:-$PROJECT_ROOT/oss-fuzz}"
REGISTRY_DIR="${REGISTRY_DIR:-$PROJECT_ROOT/crses/registry}"
CRS_CONFIGS_DIR="${CRS_CONFIGS_DIR:-$PROJECT_ROOT/crses/configs}"
BENCHMARKS_ROOT="${BENCHMARKS_ROOT:-$PROJECT_ROOT/benchmarks}"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== CRSBench Local Integration Test ===${NC}"
echo ""

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Error: Config file not found: $CONFIG_FILE${NC}"
    exit 1
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  Config file: $CONFIG_FILE"
echo "  Experiment name: $EXPERIMENT_NAME"
echo "  Mode: Local (no distributed execution)"
echo "  OSS-Fuzz path: $OSS_FUZZ_PATH"
echo "  Registry directory: $REGISTRY_DIR"
echo "  CRS configs directory: $CRS_CONFIGS_DIR"
echo "  Benchmarks root: $BENCHMARKS_ROOT"
echo ""

# Set up virtual environment
echo -e "${YELLOW}Setting up virtual environment...${NC}"
cd "$PROJECT_ROOT"
if [ ! -d ".venv" ]; then
    echo "Creating new virtual environment..."
    uv sync
else
    echo "Virtual environment exists, syncing dependencies..."
    uv sync
fi
echo ""

# Clean up previous test data
echo -e "${YELLOW}Cleaning up previous test data...${NC}"
rm -rf /tmp/crsbench-integration-test/

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source "$PROJECT_ROOT/.venv/bin/activate"
echo ""

# Run the experiment
echo -e "${GREEN}Running CRSBench experiment...${NC}"
echo ""

crsbench \
   --experiment-config "$CONFIG_FILE" \
   --experiment-name "$EXPERIMENT_NAME" \
   --benchmarks atlanta-nasm-delta-01 \
   --crses mock-crs \
   --oss-fuzz-path "$OSS_FUZZ_PATH" \
   --registry-dir "$REGISTRY_DIR" \
   --crs-configs-dir "$CRS_CONFIGS_DIR" \
   --benchmarks-root "$BENCHMARKS_ROOT" \
   --debug

# Check exit status
if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}=== Integration test completed successfully ===${NC}"
    echo ""
    echo "Results stored in:"
    echo "  Experiment data: /tmp/crsbench-integration-test/experiment-data"
    echo "  Reports: /tmp/crsbench-integration-test/report-data"

    [ -d /tmp/crsbench-integration-test/ ] && tree -L 4 /tmp/crsbench-integration-test/

    exit 0
else
    echo ""
    echo -e "${RED}=== Integration test failed ===${NC}"

    [ -d /tmp/crsbench-integration-test/ ] && tree -L 4 /tmp/crsbench-integration-test/

    exit 1
fi

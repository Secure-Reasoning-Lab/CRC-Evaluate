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
echo ""


# Clean up previous test data
echo -e "${YELLOW}Cleaning up previous test data...${NC}"
rm -rf /tmp/crsbench-integration-test/

# Run the experiment
echo -e "${GREEN}Running CRSBench experiment...${NC}"
echo ""

uv run crsbench \
   --experiment-config "$CONFIG_FILE" \
   --experiment-name "$EXPERIMENT_NAME" \
   --benchmarks atlanta-nasm-delta-01 \
   --crses mock-crs

# Check exit status
if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}=== Integration test completed successfully ===${NC}"
    echo ""
    echo "Results stored in:"
    echo "  Experiment data: /tmp/crsbench-integration-test/experiment-data"
    echo "  Reports: /tmp/crsbench-integration-test/report-data"

    [ -d /tmp/crsbench-integration-test/ ] && tree /tmp/crsbench-integration-test/

    exit 0
else
    echo ""
    echo -e "${RED}=== Integration test failed ===${NC}"

    [ -d /tmp/crsbench-integration-test/ ] && tree /tmp/crsbench-integration-test/

    exit 1
fi

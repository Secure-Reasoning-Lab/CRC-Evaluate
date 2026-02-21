#!/bin/bash
# Integration test script for local execution (no distributed execution)
#
# This script runs a quick integration test of CRSBench using:
# - Local execution mode (no Valkey/Redis)
# - crs-atljazzer CRS
# - sanity-mock-java-spam-delta-01 benchmark
# - Minimal time limits for fast testing
#
# All configuration is in the experiment config YAML file.

set -e  # Exit on error

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
CONFIG_FILE="$SCRIPT_DIR/test-experiment-config-atljazzer.yaml"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== CRSBench Local Integration Test (atljazzer) ===${NC}"
echo ""

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Error: Config file not found: $CONFIG_FILE${NC}"
    exit 1
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  Config file: $CONFIG_FILE"
echo "  Mode: Local (no distributed execution)"
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
TEST_DIR="/tmp/crsbench-integration-test-atljazzer/"
if [ -n "$TEST_DIR" ] && [ -d "$TEST_DIR" ]; then
    rm -rf "$TEST_DIR"
fi

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source "$PROJECT_ROOT/.venv/bin/activate"
echo ""

# Run the experiment
echo -e "${GREEN}Running CRSBench experiment...${NC}"
echo ""

crsbench run \
   --experiment-config "$CONFIG_FILE" \
   --gitcache \
   --verbose

# Check exit status
if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}=== Integration test completed successfully ===${NC}"
    echo ""
    echo "Results stored in:"
    echo "  Experiment data: /tmp/crsbench-integration-test-atljazzer/experiment-data"
    echo "  Reports: /tmp/crsbench-integration-test-atljazzer/report-data"

    [ -d /tmp/crsbench-integration-test-atljazzer/ ] && tree -L 4 /tmp/crsbench-integration-test-atljazzer/

    exit 0
else
    echo ""
    echo -e "${RED}=== Integration test failed ===${NC}"

    [ -d /tmp/crsbench-integration-test-atljazzer/ ] && tree -L 4 /tmp/crsbench-integration-test-atljazzer/

    exit 1
fi

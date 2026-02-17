#!/bin/bash
# Integration test script for patch CRS (bug-fixing type)
#
# This script runs a quick integration test of CRSBench using:
# - Local execution mode (no Valkey/Redis)
# - atlantis-multi-retrieval CRS (bug-fixing type)
# - atlanta-nasm-delta-01 benchmark
# - Note: atlantis-multi-retrieval requires LiteLLM (set UPSTREAM_LITELLM_BASE_URL and LITELLM_API_KEY)
#
# All configuration is in the experiment config YAML file.

set -e  # Exit on error

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
CONFIG_FILE="$SCRIPT_DIR/test-experiment-config-patch-multi-retrieval.yaml"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== CRSBench Patch CRS Integration Test (atlantis-multi-retrieval) ===${NC}"
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
TEST_DIR="/tmp/crsbench-integration-test-patch-multi-retrieval/"
if [ -n "$TEST_DIR" ] && [ -d "$TEST_DIR" ]; then
    rm -rf "$TEST_DIR"
fi

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source "$PROJECT_ROOT/.venv/bin/activate"
echo ""

# Run the experiment
echo -e "${GREEN}Running CRSBench patch CRS experiment...${NC}"
echo ""

crsbench run \
   --experiment-config "$CONFIG_FILE" \
   --verbose

# Check exit status
if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}=== Integration test completed successfully ===${NC}"
    echo ""
    echo "Results stored in:"
    echo "  Experiment data: /tmp/crsbench-integration-test-patch-multi-retrieval/experiment-data"
    echo "  Reports: /tmp/crsbench-integration-test-patch-multi-retrieval/report-data"

    # Show directory structure (exclude crs-build and crs-input which are large)
    echo ""
    echo "Directory structure:"
    [ -d /tmp/crsbench-integration-test-patch-multi-retrieval/ ] && tree -L 12 -I 'crs-build|crs-input|snapshot*' /tmp/crsbench-integration-test-patch-multi-retrieval/

    exit 0
else
    echo ""
    echo -e "${RED}=== Integration test failed ===${NC}"

    # Show directory structure (exclude crs-build and crs-input which are large)
    echo ""
    echo "Directory structure:"
    [ -d /tmp/crsbench-integration-test-patch-multi-retrieval/ ] && tree -L 12 -I 'crs-build|crs-input|snapshot*' /tmp/crsbench-integration-test-patch-multi-retrieval/

    exit 1
fi

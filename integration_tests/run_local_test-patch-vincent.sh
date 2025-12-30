#!/bin/bash
# Integration test script for patch CRS (bug-fixing type)
#
# This script runs a quick integration test of CRSBench using:
# - Local execution mode (no Valkey/Redis)
# - atlantis-vincent CRS (bug-fixing type)
# - atlanta-nasm-delta-01 benchmark
# - Note: atlantis-vincent requires LiteLLM (set UPSTREAM_LITELLM_BASE_URL and LITELLM_API_KEY)

set -e  # Exit on error

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
CONFIG_FILE="$SCRIPT_DIR/test-experiment-config-patch-vincent.yaml"
EXPERIMENT_NAME="integration-test-patch-vincent"
CRS_NAME="atlantis-vincent"

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

echo -e "${GREEN}=== CRSBench Patch CRS Integration Test ($CRS_NAME) ===${NC}"
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
echo "  CRS: $CRS_NAME (bug-fixing type)"
echo "  Benchmark: atlanta-nasm-delta-01"
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
TEST_DIR="/tmp/crsbench-$EXPERIMENT_NAME/"
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

crsbench \
   --experiment-config "$CONFIG_FILE" \
   --experiment-name "$EXPERIMENT_NAME" \
   --benchmarks atlanta-nasm-delta-01 \
   --crses "$CRS_NAME" \
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
    echo "  Experiment data: /tmp/crsbench-$EXPERIMENT_NAME/experiment-data"
    echo "  Reports: /tmp/crsbench-$EXPERIMENT_NAME/report-data"

    # Show directory structure (exclude crs-build and crs-input which are large)
    echo ""
    echo "Directory structure:"
    [ -d /tmp/crsbench-$EXPERIMENT_NAME/ ] && tree -L 12 -I 'crs-build|crs-input|snapshot*' /tmp/crsbench-$EXPERIMENT_NAME/

    exit 0
else
    echo ""
    echo -e "${RED}=== Integration test failed ===${NC}"

    # Show directory structure (exclude crs-build and crs-input which are large)
    echo ""
    echo "Directory structure:"
    [ -d /tmp/crsbench-$EXPERIMENT_NAME/ ] && tree -L 12 -I 'crs-build|crs-input|snapshot*' /tmp/crsbench-$EXPERIMENT_NAME/

    exit 1
fi

#!/bin/bash
# Distributed integration test script for CRSBench
#
# This script runs a distributed integration test of CRSBench using:
# - Distributed execution mode (Redis/Valkey required)
# - crsbench worker command for background worker process
# - crs-libfuzzer CRS
# - atlanta-nasm-delta-01 benchmark
# - Minimal time limits for fast testing
#
# Usage:
#   ./run_distributed_test-libfuzzer.sh [--tmux]
#
# Options:
#   --tmux    Launch worker in a new tmux vertical pane (requires tmux)

set -e  # Exit on error

# Parse command line arguments
USE_TMUX=false
for arg in "$@"; do
    case $arg in
        --tmux)
            USE_TMUX=true
            shift
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--tmux]"
            exit 1
            ;;
    esac
done

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
CONFIG_FILE="$SCRIPT_DIR/test-experiment-config-libfuzzer.yaml"
EXPERIMENT_NAME="integration-test-distributed-libfuzzer"

# Path overrides (CLI arguments have highest precedence)
OSS_FUZZ_PATH="${OSS_FUZZ_PATH:-$PROJECT_ROOT/oss-fuzz}"
REGISTRY_DIR="${REGISTRY_DIR:-$PROJECT_ROOT/crses/registry}"
CRS_CONFIGS_DIR="${CRS_CONFIGS_DIR:-$PROJECT_ROOT/crses/configs}"
BENCHMARKS_ROOT="${BENCHMARKS_ROOT:-$PROJECT_ROOT/benchmarks}"

# Worker configuration
TEST_DIR="/tmp/crsbench-distributed-test-libfuzzer"
WORKER_LOG="$TEST_DIR/worker.log"
WORKER_PID=""
TMUX_PANE_ID=""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Cleanup function
cleanup() {
    if [ "$USE_TMUX" = true ] && [ -n "$TMUX_PANE_ID" ]; then
        echo -e "${YELLOW}Killing tmux pane (${TMUX_PANE_ID})...${NC}"
        tmux kill-pane -t "$TMUX_PANE_ID" 2>/dev/null || true
    elif [ -n "$WORKER_PID" ]; then
        echo -e "${YELLOW}Stopping worker process (PID: $WORKER_PID)...${NC}"
        kill "$WORKER_PID" 2>/dev/null || true
        wait "$WORKER_PID" 2>/dev/null || true
    fi
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

echo -e "${GREEN}=== CRSBench Distributed Integration Test (libFuzzer) ===${NC}"
echo ""

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Error: Config file not found: $CONFIG_FILE${NC}"
    exit 1
fi

# Check if Redis is running
echo -e "${YELLOW}Checking Redis availability...${NC}"
if ! redis-cli ping > /dev/null 2>&1; then
    echo -e "${RED}Error: Redis is not running or not accessible${NC}"
    echo "Please start Redis server before running this test:"
    echo "  sudo systemctl start redis"
    echo "  # or"
    echo "  docker run -d -p 6379:6379 redis:latest"
    exit 1
fi
echo -e "${GREEN}✓ Redis is running${NC}"
echo ""

# Check if tmux is available when --tmux flag is used
if [ "$USE_TMUX" = true ]; then
    echo -e "${YELLOW}Checking tmux availability...${NC}"
    if ! command -v tmux &> /dev/null; then
        echo -e "${RED}Error: tmux is not installed${NC}"
        echo "Please install tmux or run without --tmux flag:"
        echo "  sudo apt-get install tmux  # Debian/Ubuntu"
        echo "  sudo yum install tmux      # RHEL/CentOS"
        exit 1
    fi

    # Check if we're inside a tmux session
    if [ -z "$TMUX" ]; then
        echo -e "${RED}Error: Not running inside a tmux session${NC}"
        echo "Please run this script from within tmux when using --tmux flag:"
        echo "  tmux"
        echo "  ./run_distributed_test-libfuzzer.sh --tmux"
        exit 1
    fi
    echo -e "${GREEN}✓ tmux is available${NC}"
    echo ""
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  Config file: $CONFIG_FILE"
echo "  Experiment name: $EXPERIMENT_NAME"
echo "  Mode: Distributed (Redis-based job queue)"
if [ "$USE_TMUX" = true ]; then
    echo "  Worker display: tmux vertical pane"
else
    echo "  Worker display: background process"
    echo "  Worker log: $WORKER_LOG"
fi
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
if [ -n "$TEST_DIR" ] && [ -d "$TEST_DIR" ]; then
    rm -rf "$TEST_DIR"
fi
mkdir -p "$TEST_DIR"
echo ""

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source "$PROJECT_ROOT/.venv/bin/activate"
echo ""

# Start worker in continuous mode (waits for jobs)
echo -e "${YELLOW}Starting distributed worker in continuous mode...${NC}"

if [ "$USE_TMUX" = true ]; then
    # Launch worker in a new tmux vertical pane with tee for logging
    WORKER_CMD="cd '$PROJECT_ROOT' && source .venv/bin/activate && crsbench worker --redis-host localhost --experiment-name '$EXPERIMENT_NAME' --log-level INFO --continuous 2>&1 | tee '$WORKER_LOG'"

    # Split window vertically and run worker in new pane
    tmux split-window -h "$WORKER_CMD"

    # Get the pane ID (the newly created pane)
    TMUX_PANE_ID=$(tmux list-panes -F '#{pane_id}' | tail -n 1)

    echo -e "${GREEN}✓ Worker started in tmux pane: ${TMUX_PANE_ID}${NC}"
    echo "  Mode: Continuous (will wait for jobs)"
    echo "  Worker output is visible in the right pane"
    echo "  Worker log: $WORKER_LOG"
    echo ""
else
    # Launch worker as background process
    crsbench worker \
        --redis-host localhost \
        --experiment-name "$EXPERIMENT_NAME" \
        --log-level INFO \
        --continuous \
        > "$WORKER_LOG" 2>&1 &
    WORKER_PID=$!

    echo -e "${GREEN}✓ Worker started (PID: $WORKER_PID)${NC}"
    echo "  Worker log: $WORKER_LOG"
    echo "  Mode: Continuous (will wait for jobs)"
    echo ""

    # Give worker a moment to connect
    sleep 2

    # Check if worker is still running
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        echo -e "${RED}Error: Worker process died immediately${NC}"
        echo ""
        echo -e "${YELLOW}Worker log output:${NC}"
        cat "$WORKER_LOG" || echo "(log file not found)"
        exit 1
    fi
fi

# Run the experiment in distributed mode (enqueues jobs)
echo -e "${GREEN}Running CRSBench coordinator (enqueuing jobs)...${NC}"
echo ""

EXPERIMENT_EXIT_CODE=0
crsbench run \
    --experiment-config "$CONFIG_FILE" \
    --crses crs-libfuzzer \
    --oss-fuzz-path "$OSS_FUZZ_PATH" \
    --registry-dir "$REGISTRY_DIR" \
    --crs-configs-dir "$CRS_CONFIGS_DIR" \
    --benchmarks-root "$BENCHMARKS_ROOT" \
    --distributed \
    --gitcache \
    --debug || EXPERIMENT_EXIT_CODE=$?

echo -e "${GREEN}✓ Coordinator finished${NC}"
echo ""

# Stop the worker (continuous mode doesn't exit on its own)
echo -e "${YELLOW}Stopping worker...${NC}"
if [ "$USE_TMUX" = true ]; then
    if [ -n "$TMUX_PANE_ID" ]; then
        tmux kill-pane -t "$TMUX_PANE_ID" 2>/dev/null || true
        TMUX_PANE_ID=""  # Clear pane ID so cleanup doesn't try to kill it again
        echo -e "${GREEN}✓ Worker pane closed${NC}"
    fi
else
    if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
        kill "$WORKER_PID" 2>/dev/null || true
        # Give it a moment to shutdown gracefully
        sleep 2
        # Force kill if still running
        if kill -0 "$WORKER_PID" 2>/dev/null; then
            kill -9 "$WORKER_PID" 2>/dev/null || true
        fi
        wait "$WORKER_PID" 2>/dev/null || true
    fi
    WORKER_PID=""  # Clear PID so cleanup doesn't try to kill it again
    echo -e "${GREEN}✓ Worker stopped${NC}"
fi
echo ""

# Check results
if [ $EXPERIMENT_EXIT_CODE -eq 0 ]; then
    echo ""
    echo -e "${GREEN}=== Integration test completed successfully ===${NC}"
    echo ""
    echo "Results stored in:"
    echo "  Experiment data: $TEST_DIR/experiment-data"
    echo "  Reports: $TEST_DIR/report-data"
    if [ "$USE_TMUX" = false ]; then
        echo "  Worker log: $WORKER_LOG"
    fi
    echo ""

    if [ -d "$TEST_DIR" ]; then
        echo -e "${YELLOW}Directory structure:${NC}"
        tree -L 4 "$TEST_DIR" 2>/dev/null || ls -R "$TEST_DIR"
    fi

    exit 0
else
    echo ""
    echo -e "${RED}=== Integration test failed ===${NC}"
    echo "  Exit code: $EXPERIMENT_EXIT_CODE"
    echo ""

    # Display worker log on failure
    echo -e "${YELLOW}Worker log:${NC}"
    if [ -f "$WORKER_LOG" ]; then
        tail -n 50 "$WORKER_LOG"
    else
        echo "(Worker log not found)"
    fi
    echo ""

    # Display directory structure
    if [ -d "$TEST_DIR" ]; then
        echo -e "${YELLOW}Directory structure:${NC}"
        tree -L 4 "$TEST_DIR" 2>/dev/null || ls -R "$TEST_DIR"
    fi

    exit 1
fi

#!/bin/bash
# Unified distributed integration test script for CRSBench
#
# This script runs a distributed integration test of CRSBench using:
# - Distributed execution mode (Redis/Valkey required)
# - crsbench worker command for background worker process
# - Configurable CRS, benchmark, workers, and timeouts
#
# Usage:
#   ./run_distributed_test.sh [OPTIONS]
#
# Options:
#   -j, --workers <n>       Number of parallel workers (default: 1)
#   -c, --crs <name>        CRS name (default: crs-libfuzzer)
#   -b, --benchmark <name>  Benchmark name(s), can be specified multiple times
#                           (default: atlanta-nasm-delta-01, afc-curl-delta-02,
#                            afc-freerdp-delta-02, afc-lcms-full-01)
#   -t, --timeout <secs>    Max total time in seconds (default: 300)
#   --trials <n>            Number of trials (default: 1)
#   --tmux                  Launch worker in tmux vertical pane
#   --kill-pane             Kill the worker pane after test (with --tmux)
#   --no-cpuset             Disable CPU affinity (default: enabled, not supported with -j 1)
#   --verify                Enable verification (default: skip verification)
#   --debug                 Enable debug output from crsbench
#   --skip-cleanup          Don't delete generated config file after test
#   -h, --help              Show this help message
#
# Examples:
#   ./run_distributed_test.sh
#   ./run_distributed_test.sh -j 2 --tmux
#   ./run_distributed_test.sh -b atlanta-nasm-delta-01 -b afc-curl-delta-02
#   ./run_distributed_test.sh --crs crs-libfuzzer --timeout 600
#   ./run_distributed_test.sh -j 4 --debug
#   ./run_distributed_test.sh -j 4 --verify --debug
#   ./run_distributed_test.sh -j 4 --debug --skip-cleanup

set -e  # Exit on error

# Default values
NUM_WORKERS=1
CRS_NAME="crs-libfuzzer"
BENCHMARKS=()
BENCHMARKS_SET=false
MAX_TOTAL_TIME=300
TRIALS=1
USE_TMUX=false
KILL_PANE=false
CPUSET=true
DEBUG=false
SKIP_CLEANUP=false
SKIP_VERIFICATION=true

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -j|--workers)
            NUM_WORKERS="$2"
            shift 2
            ;;
        -c|--crs)
            CRS_NAME="$2"
            shift 2
            ;;
        -b|--benchmark)
            if [ "$BENCHMARKS_SET" = false ]; then
                BENCHMARKS=()  # Clear default
                BENCHMARKS_SET=true
            fi
            BENCHMARKS+=("$2")
            shift 2
            ;;
        -t|--timeout)
            MAX_TOTAL_TIME="$2"
            shift 2
            ;;
        --trials)
            TRIALS="$2"
            shift 2
            ;;
        --tmux)
            USE_TMUX=true
            shift
            ;;
        --kill-pane)
            KILL_PANE=true
            shift
            ;;
        --no-cpuset)
            CPUSET=false
            shift
            ;;
        --debug)
            DEBUG=true
            shift
            ;;
        --skip-cleanup)
            SKIP_CLEANUP=true
            shift
            ;;
        --verify)
            SKIP_VERIFICATION=false
            shift
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

# Apply default benchmarks if none were specified
if [ "$BENCHMARKS_SET" = false ]; then
    BENCHMARKS=(
        "atlanta-nasm-delta-01"
        "afc-curl-delta-02"
        "afc-freerdp-delta-02"
        "afc-lcms-full-01"
    )
fi

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Dynamic experiment name based on worker count
if [ "$NUM_WORKERS" -eq 1 ]; then
    EXPERIMENT_NAME="integration-test-distributed-single"
    TEST_DIR="/tmp/crsbench-distributed-test-single"
else
    EXPERIMENT_NAME="integration-test-distributed-parallel-${NUM_WORKERS}w"
    TEST_DIR="/tmp/crsbench-distributed-test-parallel-${NUM_WORKERS}w"
fi

# Configuration
CONFIG_DIR="$SCRIPT_DIR/.generated-configs"
CONFIG_FILE="$CONFIG_DIR/test-experiment-config-distributed-${EXPERIMENT_NAME}.yaml"

# Path overrides (environment variables or defaults)
OSS_FUZZ_PATH="${OSS_FUZZ_PATH:-$PROJECT_ROOT/oss-fuzz}"
REGISTRY_DIR="${REGISTRY_DIR:-$PROJECT_ROOT/crses/registry}"
CRS_CONFIGS_DIR="${CRS_CONFIGS_DIR:-$PROJECT_ROOT/crses/configs}"
BENCHMARKS_ROOT="${BENCHMARKS_ROOT:-$PROJECT_ROOT/benchmarks}"

# Worker configuration
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
    # Stop worker
    if [ "$USE_TMUX" = true ] && [ -n "$TMUX_PANE_ID" ] && [ "$KILL_PANE" = true ]; then
        echo -e "${YELLOW}Killing tmux pane (${TMUX_PANE_ID})...${NC}"
        tmux kill-pane -t "$TMUX_PANE_ID" 2>/dev/null || true
    elif [ -n "$WORKER_PID" ]; then
        echo -e "${YELLOW}Stopping worker process (PID: $WORKER_PID)...${NC}"
        kill "$WORKER_PID" 2>/dev/null || true
        wait "$WORKER_PID" 2>/dev/null || true
    fi

    # Clean up config file
    if [ "$SKIP_CLEANUP" = false ]; then
        echo -e "${YELLOW}Cleaning up generated config file...${NC}"
        rm -f "$CONFIG_FILE"
        # Remove config dir if empty
        rmdir "$CONFIG_DIR" 2>/dev/null || true
    else
        echo -e "${YELLOW}Skipping config cleanup (--skip-cleanup specified)${NC}"
        echo "  Config file: $CONFIG_FILE"
    fi
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

echo -e "${GREEN}=== CRSBench Distributed Integration Test ===${NC}"
echo ""

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
        echo "  ./run_distributed_test.sh --tmux"
        exit 1
    fi
    echo -e "${GREEN}✓ tmux is available${NC}"
    echo ""
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  CRS: $CRS_NAME"
echo "  Benchmarks: ${BENCHMARKS[*]}"
echo "  Experiment name: $EXPERIMENT_NAME"
echo "  Max total time: ${MAX_TOTAL_TIME}s"
echo "  Trials: $TRIALS"
echo "  Mode: Distributed (Redis-based job queue)"
echo "  Debug mode: $DEBUG"
echo "  Parallel workers: $NUM_WORKERS"
echo "  CPU affinity (cpuset): $CPUSET"
echo "  Skip verification: $SKIP_VERIFICATION"
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

# Create config directory if it doesn't exist
mkdir -p "$CONFIG_DIR"

# Generate config file
echo -e "${YELLOW}Generating config file: $CONFIG_FILE${NC}"
cat > "$CONFIG_FILE" << EOF
# Auto-generated test configuration for distributed integration tests
# Generated by: run_distributed_test.sh
# CRS: $CRS_NAME
# Benchmarks: ${BENCHMARKS[*]}
# Workers: $NUM_WORKERS

experiment: "$EXPERIMENT_NAME"
description: "Distributed integration test for $CRS_NAME with $NUM_WORKERS worker(s)"

# Execution Configuration
trials: $TRIALS
mode: delta
max_total_time: $MAX_TOTAL_TIME

# CRS Selection
crses:
  - $CRS_NAME

# Benchmark Selection
benchmarks:
EOF

# Add each benchmark to the config file
for bench in "${BENCHMARKS[@]}"; do
    echo "  - $bench" >> "$CONFIG_FILE"
done

cat >> "$CONFIG_FILE" << EOF

# Difficulty Control
difficulty_level: 1

# Monitoring & Snapshots
snapshot_period: 60

# Storage Configuration
experiment_filestore: $TEST_DIR/experiment-data
report_filestore: $TEST_DIR/report-data

# LiteLLM Configuration
litellm_mode: passthrough

# Build Configuration
build_timeout: 600
run_timeout: $MAX_TOTAL_TIME

# Hints Configuration
hints_enabled: false

# Verification Configuration
skip_verification: $SKIP_VERIFICATION

# Redis Configuration
redis_host: localhost
EOF

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

# Build worker command
if [ "$NUM_WORKERS" -gt 1 ]; then
    WORKER_BASE_CMD="crsbench worker -j$NUM_WORKERS --redis-host localhost --experiment-name '$EXPERIMENT_NAME' --log-level INFO --continuous"
else
    WORKER_BASE_CMD="crsbench worker --redis-host localhost --experiment-name '$EXPERIMENT_NAME' --log-level INFO --continuous"
fi

# Add --no-cpuset flag if disabled
if [ "$CPUSET" = false ]; then
    WORKER_BASE_CMD="$WORKER_BASE_CMD --no-cpuset"
fi

if [ "$USE_TMUX" = true ]; then
    # Launch worker in a new tmux vertical pane with tee for logging
    # Keep pane open after worker exits by starting a new bash shell
    WORKER_CMD="cd '$PROJECT_ROOT' && source .venv/bin/activate && $WORKER_BASE_CMD 2>&1 | tee '$WORKER_LOG'; exec bash"

    # Split window vertically and run worker in new pane
    tmux split-window -h "$WORKER_CMD"

    # Get the pane ID (the newly created pane)
    TMUX_PANE_ID=$(tmux list-panes -F '#{pane_id}' | tail -n 1)

    if [ "$NUM_WORKERS" -eq 1 ]; then
        echo -e "${GREEN}✓ Worker started in tmux pane: ${TMUX_PANE_ID}${NC}"
    else
        echo -e "${GREEN}✓ Workers started in tmux pane: ${TMUX_PANE_ID}${NC}"
    fi
    echo "  Parallel workers: $NUM_WORKERS"
    echo "  Mode: Continuous (will wait for jobs)"
    echo "  Worker output is visible in the right pane"
    echo "  Worker log: $WORKER_LOG"
    echo ""
else
    # Launch worker as background process
    eval "$WORKER_BASE_CMD" > "$WORKER_LOG" 2>&1 &
    WORKER_PID=$!

    if [ "$NUM_WORKERS" -eq 1 ]; then
        echo -e "${GREEN}✓ Worker started (PID: $WORKER_PID)${NC}"
    else
        echo -e "${GREEN}✓ Workers started (PID: $WORKER_PID)${NC}"
    fi
    echo "  Parallel workers: $NUM_WORKERS"
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

# Build crsbench run command
CRSBENCH_CMD="crsbench run \
    --experiment-config \"$CONFIG_FILE\" \
    --crses \"$CRS_NAME\" \
    --oss-fuzz-path \"$OSS_FUZZ_PATH\" \
    --registry-dir \"$REGISTRY_DIR\" \
    --crs-configs-dir \"$CRS_CONFIGS_DIR\" \
    --benchmarks-root \"$BENCHMARKS_ROOT\" \
    --distributed \
    --gitcache"

if [ "$DEBUG" = true ]; then
    CRSBENCH_CMD="$CRSBENCH_CMD --debug"
fi

eval $CRSBENCH_CMD || EXPERIMENT_EXIT_CODE=$?

echo -e "${GREEN}✓ Coordinator finished${NC}"
echo ""

# Stop the worker (continuous mode doesn't exit on its own)
echo -e "${YELLOW}Stopping worker...${NC}"
if [ "$USE_TMUX" = true ]; then
    if [ -n "$TMUX_PANE_ID" ]; then
        if [ "$KILL_PANE" = true ]; then
            tmux kill-pane -t "$TMUX_PANE_ID" 2>/dev/null || true
            echo -e "${GREEN}✓ Worker pane closed${NC}"
        else
            echo -e "${GREEN}✓ Worker pane preserved (${TMUX_PANE_ID})${NC}"
            echo -e "${YELLOW}  Use 'tmux kill-pane -t ${TMUX_PANE_ID}' to close manually${NC}"
        fi
        TMUX_PANE_ID=""  # Clear pane ID so cleanup doesn't try to kill it again
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

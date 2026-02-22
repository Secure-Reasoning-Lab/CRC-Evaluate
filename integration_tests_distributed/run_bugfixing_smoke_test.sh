#!/bin/bash
# Bug-fixing smoke test for CRSBench distributed execution
#
# This script runs the bug-fixing smoke test using:
# - atlantis-vincent, atlantis-prism, atlantis-claude-code CRSes
# - 8 CPU cores, 32GB RAM per CRS
# - Distributed execution mode (Redis required)
#
# Required environment variables:
#   CRSBENCH_LLM_UPSTREAM_BASE_URL  LiteLLM server URL (e.g., https://litellm.example.com/v1)
#   CRSBENCH_LLM_API_KEY            LiteLLM API key
#
# Usage:
#   ./run_bugfixing_smoke_test.sh [OPTIONS]
#
# Options:
#   -j, --workers <n>       Number of parallel workers (default: 1)
#   --tmux                  Launch worker in tmux vertical pane
#   --kill-pane             Kill the worker pane after test (with --tmux)
#   --verbose               Enable verbose output from crsbench
#   --skip-cleanup          Don't restore original resource configs after test
#   -h, --help              Show this help message

set -e

# Default values
NUM_WORKERS=1
USE_TMUX=false
KILL_PANE=false
VERBOSE=false
SKIP_CLEANUP=false

# Resource constraints
CPU_CORES="0-7"  # 8 cores
MEMORY="32G"     # 32GB RAM

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -j|--workers)
            NUM_WORKERS="$2"
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
        --verbose)
            VERBOSE=true
            shift
            ;;
        --skip-cleanup)
            SKIP_CLEANUP=true
            shift
            ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
EXPERIMENT_NAME="bugfixing-smoke-test"
TEST_DIR="/tmp/bugfixing-smoke-test"
CONFIG_DIR="$SCRIPT_DIR/.generated-configs"
EXPERIMENT_CONFIG="$CONFIG_DIR/bugfixing-smoke-test.yaml"

# Benchmarks to test
BENCHMARKS=(
    "afc-mongoose-delta-01"
    "afc-libpng-delta-01"
)

# CRS list and their config directories
CRSES=("atlantis-vincent" "atlantis-prism" "atlantis-claude-code")
CRS_CONFIGS_DIR="$PROJECT_ROOT/crses/configs"

# Path overrides (environment variables or defaults)
OSS_FUZZ_PATH="${OSS_FUZZ_PATH:-$PROJECT_ROOT/oss-fuzz}"
REGISTRY_DIR="${REGISTRY_DIR:-$PROJECT_ROOT/crses/registry}"
BENCHMARKS_ROOT="${BENCHMARKS_ROOT:-$PROJECT_ROOT/benchmarks}"

# Worker configuration
WORKER_LOG="$TEST_DIR/worker.log"
WORKER_PID=""
TMUX_PANE_ID=""

# Track backed up configs
BACKED_UP_CONFIGS=()

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to update resource config for a CRS
update_resource_config() {
    local crs_name="$1"
    local config_file="$CRS_CONFIGS_DIR/$crs_name/config-resource.yaml"
    local backup_file="$CRS_CONFIGS_DIR/$crs_name/config-resource.yaml.backup"

    if [ -f "$config_file" ]; then
        # Backup original config
        cp "$config_file" "$backup_file"
        BACKED_UP_CONFIGS+=("$config_file")
        echo -e "  ${YELLOW}Backed up: $config_file${NC}"

        # Create new resource config
        cat > "$config_file" << EOF
# Resource Configuration for $crs_name CRS Deployment
# Modified by run_bugfixing_smoke_test.sh

# Worker definitions
workers:
  local:
    cpuset: "$CPU_CORES"
    memory: "$MEMORY"

# CRS-specific configurations
crs:
  $crs_name:
    workers:
      - local
EOF
        echo -e "  ${GREEN}Updated: $config_file (cpuset: $CPU_CORES, memory: $MEMORY)${NC}"
    else
        echo -e "  ${RED}Warning: Config not found: $config_file${NC}"
    fi
}

# Function to restore resource configs
restore_resource_configs() {
    for config_file in "${BACKED_UP_CONFIGS[@]}"; do
        local backup_file="${config_file}.backup"
        if [ -f "$backup_file" ]; then
            mv "$backup_file" "$config_file"
            echo -e "  ${GREEN}Restored: $config_file${NC}"
        fi
    done
}

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

    # Restore resource configs and cleanup generated config
    if [ "$SKIP_CLEANUP" = false ]; then
        echo -e "${YELLOW}Restoring original resource configs...${NC}"
        restore_resource_configs
        echo -e "${YELLOW}Cleaning up generated config file...${NC}"
        rm -f "$EXPERIMENT_CONFIG"
        rmdir "$CONFIG_DIR" 2>/dev/null || true
    else
        echo -e "${YELLOW}Skipping config cleanup (--skip-cleanup specified)${NC}"
        echo "  Config file: $EXPERIMENT_CONFIG"
    fi
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

echo -e "${GREEN}=== CRSBench Bug-Fixing Smoke Test ===${NC}"
echo ""

# Load .env file if exists (crsbench also loads it, but we need it for validation)
if [ -f "$PROJECT_ROOT/.env" ]; then
    echo -e "${YELLOW}Loading .env file...${NC}"
    set -a  # automatically export all variables
    source "$PROJECT_ROOT/.env"
    set +a
    echo -e "${GREEN}✓ Loaded .env${NC}"
    echo ""
fi

# Check required environment variables for LiteLLM passthrough mode
echo -e "${YELLOW}Checking LiteLLM environment variables...${NC}"
if [ -z "$CRSBENCH_LLM_UPSTREAM_BASE_URL" ]; then
    echo -e "${RED}Error: CRSBENCH_LLM_UPSTREAM_BASE_URL is not set${NC}"
    echo "Bug-fixing CRS requires LiteLLM. Please set in .env or export:"
    echo "  CRSBENCH_LLM_UPSTREAM_BASE_URL=https://your-litellm-server/v1"
    echo "  CRSBENCH_LLM_API_KEY=your-api-key"
    exit 1
fi
if [ -z "$CRSBENCH_LLM_API_KEY" ]; then
    echo -e "${RED}Error: CRSBENCH_LLM_API_KEY is not set${NC}"
    echo "Bug-fixing CRS requires LiteLLM. Please set in .env or export:"
    echo "  CRSBENCH_LLM_API_KEY=your-api-key"
    exit 1
fi
echo -e "${GREEN}✓ LiteLLM environment variables are set${NC}"
echo "  CRSBENCH_LLM_UPSTREAM_BASE_URL: $CRSBENCH_LLM_UPSTREAM_BASE_URL"
echo ""

# Create config directory
mkdir -p "$CONFIG_DIR"

# Generate experiment config
echo -e "${YELLOW}Generating experiment config: $EXPERIMENT_CONFIG${NC}"
cat > "$EXPERIMENT_CONFIG" << EOF
# Auto-generated test configuration for bug-fixing smoke test
# Generated by: run_bugfixing_smoke_test.sh

experiment: "$EXPERIMENT_NAME"
description: "Bug-fixing smoke test with specific benchmarks"

# Execution Configuration
trials: 1
max_total_time: 2400  # 40 minutes total (must be > build + run + verify timeouts)

# CRS Selection
crses:
EOF

# Add CRSes to config
for crs in "${CRSES[@]}"; do
    echo "  - $crs" >> "$EXPERIMENT_CONFIG"
done

cat >> "$EXPERIMENT_CONFIG" << EOF

mode: delta

# Benchmark Selection
benchmarks:
EOF

# Add benchmarks to config
for bench in "${BENCHMARKS[@]}"; do
    echo "  - $bench" >> "$EXPERIMENT_CONFIG"
done

cat >> "$EXPERIMENT_CONFIG" << EOF

# Difficulty Control
difficulty_level: 1

# Monitoring & Snapshots
snapshot_period: 300

# Storage Configuration
experiment_filestore: $TEST_DIR/experiment-data
report_filestore: $TEST_DIR/report-data

# LiteLLM Configuration
litellm_mode: passthrough

# Build Configuration
build_timeout: 600
run_timeout: 600
verify_timeout: 600

hints_enabled: false

# Distributed Execution Configuration
redis_host: localhost
EOF

echo -e "${GREEN}✓ Config generated${NC}"
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
        exit 1
    fi
    if [ -z "$TMUX" ]; then
        echo -e "${RED}Error: Not running inside a tmux session${NC}"
        echo "Please run this script from within tmux when using --tmux flag"
        exit 1
    fi
    echo -e "${GREEN}✓ tmux is available${NC}"
    echo ""
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  Experiment config: $EXPERIMENT_CONFIG"
echo "  CRSes: ${CRSES[*]}"
echo "  Benchmarks: ${BENCHMARKS[*]}"
echo "  CPU cores: $CPU_CORES (8 cores)"
echo "  Memory: $MEMORY"
echo "  Mode: Distributed (Redis-based job queue)"
echo "  Parallel workers: $NUM_WORKERS"
echo "  Verbose mode: $VERBOSE"
if [ "$USE_TMUX" = true ]; then
    echo "  Worker display: tmux vertical pane"
else
    echo "  Worker display: background process"
    echo "  Worker log: $WORKER_LOG"
fi
echo ""

# Update resource configs for each CRS
echo -e "${YELLOW}Updating resource configs for CRSes...${NC}"
for crs in "${CRSES[@]}"; do
    update_resource_config "$crs"
done
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

# Start worker in continuous mode
echo -e "${YELLOW}Starting distributed worker in continuous mode...${NC}"

# Build worker command
if [ "$NUM_WORKERS" -gt 1 ]; then
    WORKER_BASE_CMD="crsbench worker --cores $NUM_WORKERS --redis-host localhost --experiment-name '$EXPERIMENT_NAME' --log-level INFO --continuous"
else
    WORKER_BASE_CMD="crsbench worker --redis-host localhost --experiment-name '$EXPERIMENT_NAME' --log-level INFO --continuous"
fi

if [ "$USE_TMUX" = true ]; then
    WORKER_CMD="cd '$PROJECT_ROOT' && source .venv/bin/activate && $WORKER_BASE_CMD 2>&1 | tee '$WORKER_LOG'; exec bash"
    tmux split-window -h "$WORKER_CMD"
    TMUX_PANE_ID=$(tmux list-panes -F '#{pane_id}' | tail -n 1)
    echo -e "${GREEN}✓ Worker started in tmux pane: ${TMUX_PANE_ID}${NC}"
    echo "  Worker output is visible in the right pane"
    echo ""
else
    eval "$WORKER_BASE_CMD" > "$WORKER_LOG" 2>&1 &
    WORKER_PID=$!
    echo -e "${GREEN}✓ Worker started (PID: $WORKER_PID)${NC}"
    echo "  Worker log: $WORKER_LOG"
    echo ""

    sleep 2
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        echo -e "${RED}Error: Worker process died immediately${NC}"
        echo ""
        echo -e "${YELLOW}Worker log output:${NC}"
        cat "$WORKER_LOG" || echo "(log file not found)"
        exit 1
    fi
fi

# Run the experiment in distributed mode
echo -e "${GREEN}Running CRSBench coordinator (enqueuing jobs)...${NC}"
echo ""

EXPERIMENT_EXIT_CODE=0

CRSBENCH_CMD="crsbench run \
    --experiment-config \"$EXPERIMENT_CONFIG\" \
    --distributed"

if [ "$VERBOSE" = true ]; then
    CRSBENCH_CMD="$CRSBENCH_CMD --verbose"
fi

eval $CRSBENCH_CMD || EXPERIMENT_EXIT_CODE=$?

echo -e "${GREEN}✓ Coordinator finished${NC}"
echo ""

# Stop the worker
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
        TMUX_PANE_ID=""
    fi
else
    if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
        kill "$WORKER_PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$WORKER_PID" 2>/dev/null; then
            kill -9 "$WORKER_PID" 2>/dev/null || true
        fi
        wait "$WORKER_PID" 2>/dev/null || true
    fi
    WORKER_PID=""
    echo -e "${GREEN}✓ Worker stopped${NC}"
fi
echo ""

# Check results
if [ $EXPERIMENT_EXIT_CODE -eq 0 ]; then
    echo ""
    echo -e "${GREEN}=== Bug-fixing smoke test completed successfully ===${NC}"
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
    echo -e "${RED}=== Bug-fixing smoke test failed ===${NC}"
    echo "  Exit code: $EXPERIMENT_EXIT_CODE"
    echo ""

    echo -e "${YELLOW}Worker log:${NC}"
    if [ -f "$WORKER_LOG" ]; then
        tail -n 50 "$WORKER_LOG"
    else
        echo "(Worker log not found)"
    fi
    echo ""

    if [ -d "$TEST_DIR" ]; then
        echo -e "${YELLOW}Directory structure:${NC}"
        tree -L 4 "$TEST_DIR" 2>/dev/null || ls -R "$TEST_DIR"
    fi

    exit 1
fi

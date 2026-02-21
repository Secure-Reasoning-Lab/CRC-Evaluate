#!/bin/bash
# USENIX Experiment Runner
#
# Usage:
#   ./run_usenix_exp.sh <config.yaml>
#   ./run_usenix_exp.sh <config.yaml> --tmux

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <experiment-config.yaml> [--tmux]"
    exit 1
fi

CONFIG_FILE="$1"
USE_TMUX=false
[ "$2" = "--tmux" ] && USE_TMUX=true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRSBENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CRSBENCH_ROOT"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Config file not found: $CONFIG_FILE"
    exit 1
fi

echo "Running experiment: $CONFIG_FILE"

# Check for existing jobs in Redis
JOB_COUNT=$(redis-cli KEYS "rq:job:*" 2>/dev/null | wc -l)
if [ "$JOB_COUNT" -gt 0 ]; then
    echo "Warning: Redis has $JOB_COUNT existing jobs."
    read -p "Flush all before starting? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        redis-cli FLUSHALL > /dev/null
        echo "Redis flushed."
    fi
fi

WORKER_CMD=".venv/bin/crsbench worker --experiment-config $CONFIG_FILE --continuous"

if [ "$USE_TMUX" = true ]; then
    tmux split-window -h "cd '$CRSBENCH_ROOT' && $WORKER_CMD; exec bash"
    TMUX_PANE_ID=$(tmux list-panes -F '#{pane_id}' | tail -n 1)
    echo "Worker started in tmux pane: $TMUX_PANE_ID"
else
    $WORKER_CMD &
    WORKER_PID=$!
    echo "Worker started (PID: $WORKER_PID)"
fi

sleep 3

.venv/bin/crsbench run --experiment-config "$CONFIG_FILE" --distributed

echo "Experiment complete."

if [ "$USE_TMUX" = true ] && [ -n "$TMUX_PANE_ID" ]; then
    tmux kill-pane -t "$TMUX_PANE_ID" 2>/dev/null || true
elif [ -n "$WORKER_PID" ]; then
    kill "$WORKER_PID" 2>/dev/null || true
fi

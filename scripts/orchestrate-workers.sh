#!/usr/bin/env bash
# =============================================================================
# Worker Orchestration Script
# =============================================================================
# Run from cyclonus to push code, setup, and manage workers on remote machines.
#
# Usage:
#   scripts/orchestrate-workers.sh <command> [machines...]
#
# Commands:
#   push        Push feat/distributed branch to GitHub
#   redis-setup Configure Redis on cyclonus with password auth
#   setup       Setup workers on remote machines (parallel)
#   start       Start workers on remote machines via tmux
#   stop        Stop workers on remote machines
#   status      Check worker status on remote machines
#   logs        Tail worker logs on a remote machine
#   collect     Rsync experiment results from remote machines to cyclonus
#   all         Push + setup + start (full deployment)
#
# Environment:
#   REDIS_PASSWORD  Override Redis password (default: read from ~/.crsbench-redis-password)
#
# Examples:
#   scripts/orchestrate-workers.sh redis-setup            # One-time Redis setup
#   scripts/orchestrate-workers.sh all                    # Deploy to all machines
#   scripts/orchestrate-workers.sh setup cerebros ramjet  # Setup specific machines
#   scripts/orchestrate-workers.sh start cerebros         # Start one machine
#   scripts/orchestrate-workers.sh status                 # Check all machines
#   scripts/orchestrate-workers.sh logs ramjet            # Tail logs on ramjet
#   scripts/orchestrate-workers.sh stop                   # Stop all workers
#   scripts/orchestrate-workers.sh collect                # Rsync results to cyclonus
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BRANCH="feat/distributed"
SETUP_SCRIPT="$SCRIPT_DIR/setup-remote-worker.sh"
INSTALL_DIR="/home/dongkwan/CRSBench"
TMUX_SESSION="crsbench-worker"
REMOTE_EXPERIMENT_DIR="/home/dongkwan/crsbench_eval_given_fuzzer/experiment-data-afc2"
LOCAL_COLLECT_DIR="/home/dongkwan/crsbench_eval_given_fuzzer/collected-results"
REDIS_PASSWORD_FILE="$HOME/.crsbench-redis-password"

# Default worker machines
ALL_MACHINES=(cerebros ramjet)

# Machine hostname mapping
declare -A HOSTNAMES
HOSTNAMES[cerebros]="cerebros.gtisc.gatech.edu"
HOSTNAMES[ramjet]="ramjet.gtisc.gatech.edu"

# SSH options: no strict host key checking, connection timeout
SSH_OPTS="-A -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

log() { echo "[orchestrate] $*"; }
err() { echo "[orchestrate] ERROR: $*" >&2; }

get_redis_password() {
    # Prefer env var, then password file
    if [[ -n "${REDIS_PASSWORD:-}" ]]; then
        echo "$REDIS_PASSWORD"
        return
    fi
    if [[ -f "$REDIS_PASSWORD_FILE" ]]; then
        cat "$REDIS_PASSWORD_FILE"
        return
    fi
    err "Redis password not found. Set REDIS_PASSWORD or run: scripts/orchestrate-workers.sh redis-setup"
    exit 1
}

get_hostname() {
    local machine="$1"
    if [[ -z "${HOSTNAMES[$machine]+x}" ]]; then
        err "Unknown machine: $machine (known: ${!HOSTNAMES[*]})"
        exit 1
    fi
    echo "${HOSTNAMES[$machine]}"
}

get_machines() {
    # Use provided machines or default to all
    if [[ $# -gt 0 ]]; then
        echo "$@"
    else
        echo "${ALL_MACHINES[@]}"
    fi
}

ssh_cmd() {
    local host="$1"
    shift
    # Separate SSH flags (starting with -) from the remote command
    local ssh_flags=()
    while [[ $# -gt 0 && "$1" == -* ]]; do
        ssh_flags+=("$1")
        shift
    done
    ssh $SSH_OPTS "${ssh_flags[@]+"${ssh_flags[@]}"}" "$host" "$@"
}

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

cmd_redis_setup() {
    log "Setting up Redis with password authentication on localhost..."

    # Generate a random password
    local password
    password=$(openssl rand -base64 24)

    # Check if Redis is installed
    if ! command -v redis-cli &>/dev/null; then
        err "redis-cli not found. Install Redis first: sudo apt install redis-server"
        exit 1
    fi

    # Find Redis config
    local redis_conf=""
    for candidate in /etc/redis/redis.conf /etc/redis.conf; do
        if [[ -f "$candidate" ]]; then
            redis_conf="$candidate"
            break
        fi
    done

    if [[ -z "$redis_conf" ]]; then
        err "Redis config not found at /etc/redis/redis.conf or /etc/redis.conf"
        exit 1
    fi

    log "  Redis config: $redis_conf"
    log "  Configuring password and network binding..."

    # Apply config via redis-cli (takes effect immediately, persists to config)
    redis-cli CONFIG SET requirepass "$password"
    # Authenticate with the new password for subsequent commands
    redis-cli -a "$password" CONFIG SET bind "0.0.0.0"
    redis-cli -a "$password" CONFIG SET protected-mode "no"
    redis-cli -a "$password" CONFIG REWRITE 2>/dev/null || true

    # Save password to file (used by get_redis_password helper)
    echo -n "$password" > "$REDIS_PASSWORD_FILE"
    chmod 600 "$REDIS_PASSWORD_FILE"

    log "  Password saved to: $REDIS_PASSWORD_FILE"
    log "  Redis is now accepting remote connections with password auth"
    log ""
    log "  Workers:  password is passed automatically by 'start' command"
    log "  Cyclonus: export REDIS_PASSWORD=\$(cat $REDIS_PASSWORD_FILE)"
}

cmd_push() {
    log "Pushing $BRANCH to origin..."
    cd "$REPO_DIR"
    git push origin "$BRANCH"
    log "Push complete"
}

cmd_setup() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    if [[ ! -f "$SETUP_SCRIPT" ]]; then
        err "Setup script not found: $SETUP_SCRIPT"
        exit 1
    fi

    log "Setting up workers on: ${machines[*]}"

    local pids=()
    local logdir
    logdir=$(mktemp -d)

    for machine in "${machines[@]}"; do
        local host
        host=$(get_hostname "$machine")
        local logfile="$logdir/$machine.log"

        log "  Starting setup on $machine ($host)..."
        # Copy script to remote then execute (preserves SSH agent forwarding
        # for git operations; piping via stdin breaks nested SSH)
        scp -q $SSH_OPTS "$SETUP_SCRIPT" "$host:/tmp/setup-remote-worker.sh"
        # Pass REDIS_PASSWORD if available so setup can test connectivity
        local redis_env=""
        if [[ -f "$REDIS_PASSWORD_FILE" ]]; then
            redis_env="REDIS_PASSWORD=$(cat "$REDIS_PASSWORD_FILE")"
        elif [[ -n "${REDIS_PASSWORD:-}" ]]; then
            redis_env="REDIS_PASSWORD=$REDIS_PASSWORD"
        fi
        ssh $SSH_OPTS "$host" "$redis_env bash /tmp/setup-remote-worker.sh $machine" \
            > "$logfile" 2>&1 &
        pids+=($!)
    done

    # Monitor progress while waiting
    log "  Setup running in parallel. Tailing progress..."
    local all_done=0
    while [[ $all_done -eq 0 ]]; do
        all_done=1
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                all_done=0
                break
            fi
        done
        if [[ $all_done -eq 0 ]]; then
            # Show latest step from each machine
            for machine in "${machines[@]}"; do
                local last_step
                last_step=$(grep -oP '\[\d+/\d+\].*' "$logdir/$machine.log" 2>/dev/null | tail -1 || true)
                if [[ -n "$last_step" ]]; then
                    echo "    $machine: $last_step"
                fi
            done
            sleep 10
        fi
    done

    # Collect exit codes
    local failed=0
    for i in "${!machines[@]}"; do
        local machine="${machines[$i]}"
        local pid="${pids[$i]}"
        local logfile="$logdir/$machine.log"

        if wait "$pid"; then
            log "  $machine: setup OK"
        else
            err "  $machine: setup FAILED (see $logfile)"
            failed=1
        fi
    done

    if [[ $failed -eq 1 ]]; then
        log "Log files in: $logdir"
        exit 1
    fi

    rm -rf "$logdir"
    log "All workers set up"
}

cmd_start() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    local redis_pass
    redis_pass=$(get_redis_password)

    log "Starting workers on: ${machines[*]}"

    for machine in "${machines[@]}"; do
        local host
        host=$(get_hostname "$machine")
        local config="$INSTALL_DIR/experiment-configs/experiment-config-afc-${machine}.yaml"

        # Kill existing tmux session if present
        ssh_cmd "$host" "tmux kill-session -t $TMUX_SESSION 2>/dev/null || true"

        # Start worker in tmux with REDIS_PASSWORD and PATH for uv
        ssh_cmd "$host" "tmux new-session -d -s $TMUX_SESSION \
            'export PATH=\$HOME/.local/bin:\$PATH && export REDIS_PASSWORD=\"$redis_pass\" && cd $INSTALL_DIR && uv run crsbench worker --experiment-config $config 2>&1 | tee worker.log'"

        log "  $machine: worker started (tmux session: $TMUX_SESSION)"
    done

    log "All workers started"
}

cmd_stop() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    log "Stopping workers on: ${machines[*]}"

    for machine in "${machines[@]}"; do
        local host
        host=$(get_hostname "$machine")

        if ssh_cmd "$host" "tmux kill-session -t $TMUX_SESSION 2>/dev/null"; then
            log "  $machine: worker stopped"
        else
            log "  $machine: no worker running"
        fi
    done
}

cmd_status() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    for machine in "${machines[@]}"; do
        local host
        host=$(get_hostname "$machine")

        echo ""
        echo "=== $machine ($host) ==="

        # Check if tmux session exists
        if ssh_cmd "$host" "tmux has-session -t $TMUX_SESSION 2>/dev/null"; then
            echo "  Worker: RUNNING"
        else
            echo "  Worker: STOPPED"
        fi

        # Disk usage
        ssh_cmd "$host" "df -h /home | tail -1 | awk '{print \"  Disk:   \" \$3 \" used / \" \$2 \" (\" \$5 \" full)\"}'" 2>/dev/null || true

        # Docker containers
        local containers
        containers=$(ssh_cmd "$host" "docker ps -q 2>/dev/null | wc -l" 2>/dev/null || echo "?")
        echo "  Docker: $containers containers running"
    done
    echo ""
}

cmd_logs() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    if [[ ${#machines[@]} -ne 1 ]]; then
        err "logs command requires exactly one machine (got: ${machines[*]})"
        exit 1
    fi

    local machine="${machines[0]}"
    local host
    host=$(get_hostname "$machine")

    log "Attaching to $machine worker logs (Ctrl+C to detach)..."
    ssh_cmd "$host" -t "tmux attach -t $TMUX_SESSION"
}

cmd_collect() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    log "Collecting results from: ${machines[*]}"
    log "  Remote source: $REMOTE_EXPERIMENT_DIR"
    log "  Local dest:    $LOCAL_COLLECT_DIR"

    mkdir -p "$LOCAL_COLLECT_DIR"

    for machine in "${machines[@]}"; do
        local host
        host=$(get_hostname "$machine")
        local dest="$LOCAL_COLLECT_DIR/$machine"

        mkdir -p "$dest"
        log "  Syncing $machine..."

        # Rsync experiment data, excluding build artifacts
        rsync -az --progress \
            --exclude='crs-build/' \
            --exclude='.oss-bugfind/' \
            -e "ssh $SSH_OPTS" \
            "$host:$REMOTE_EXPERIMENT_DIR/" \
            "$dest/"

        log "  $machine: sync complete → $dest"
    done

    # Show summary
    log "Collection complete. Results in $LOCAL_COLLECT_DIR:"
    du -sh "$LOCAL_COLLECT_DIR"/*/ 2>/dev/null || true
}

cmd_all() {
    local machines
    read -ra machines <<< "$(get_machines "$@")"

    cmd_push
    echo ""
    cmd_setup "${machines[@]}"
    echo ""
    cmd_start "${machines[@]}"
    echo ""
    cmd_status "${machines[@]}"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

COMMAND="${1:-help}"
shift || true

case "$COMMAND" in
    push)        cmd_push ;;
    redis-setup) cmd_redis_setup ;;
    setup)       cmd_setup "$@" ;;
    start)       cmd_start "$@" ;;
    stop)        cmd_stop "$@" ;;
    status)      cmd_status "$@" ;;
    logs)        cmd_logs "$@" ;;
    collect)     cmd_collect "$@" ;;
    all)         cmd_all "$@" ;;
    help|--help|-h)
        head -32 "$0" | tail -30
        ;;
    *)
        err "Unknown command: $COMMAND"
        head -32 "$0" | tail -30
        exit 1
        ;;
esac

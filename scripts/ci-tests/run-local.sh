#!/bin/bash
# Run CI checks locally
#
# CI runs: checks → format → sanity-mock → e2e
#
# Usage:
#   ./scripts/ci-tests/run-local.sh              # Run full CI pipeline (checks + format + mock + smoke)
#   ./scripts/ci-tests/run-local.sh checks       # Stage 1: typecheck, lint, format, unit tests
#   ./scripts/ci-tests/run-local.sh format       # Stage 2a: format validation (sanity benchmarks)
#   ./scripts/ci-tests/run-local.sh sanity       # Stage 2b: all checks (mock-c + mock-java)
#   ./scripts/ci-tests/run-local.sh e2e          # Stage 3: bug finding E2E
#   ./scripts/ci-tests/run-local.sh smoke        # Stage 4: parallel smoke (bugfinding + bugfixing)
#   ./scripts/ci-tests/run-local.sh smoke-validate-config
#   ./scripts/ci-tests/run-local.sh smoke-bugfinding
#   ./scripts/ci-tests/run-local.sh smoke-bugfixing
#
# TODO: re-enable after adding HuggingFace download to CI
#   ./scripts/ci-tests/run-local.sh all          # Run all stages including integration
#   ./scripts/ci-tests/run-local.sh sanity-real  # Stage 2c: all checks (afc-xz + json-java)
#   ./scripts/ci-tests/run-local.sh integration  # Local only: real projects (libxml2, commons-compress)
#
# Smoke config (config-first):
#   SMOKE_BUGFINDING_CONFIG=<path>               # defaults to sanity bugfinding config
#   SMOKE_BUGFIXING_CONFIG=<path>                # defaults to sanity bugfixing config
#   SMOKE_CPUSET_<SUITE_NAME>=<cpuset>           # optional per-suite cpuset override (e.g. SMOKE_CPUSET_BUGFINDING=0-23)
#   SMOKE_SKIP_CPUSET_<SUITE_NAME>=<cpuset>      # optional per-suite excluded CPUs
#   SMOKE_STREAM_LOGS=1                          # stream smoke worker/run logs live while also saving worker.log/run.log
#   CRSBENCH_RUN_LOCAL_ENV_FILE=<path>           # optional .env file to load for smoke runs (defaults to <repo>/.env)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
SMOKE_BUGFINDING_CONFIG_DEFAULT="$ROOT_DIR/experiment-configs/sanity-bugfinding/atlantis-multilang-given_fuzzer-default-delta.yaml"
SMOKE_BUGFIXING_CONFIG_DEFAULT="$ROOT_DIR/experiment-configs/sanity-bugfixing/crs-claude-code-delta-sanity-smoke.yaml"

cd "$ROOT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

run_stage() {
    echo -e "\n${YELLOW}=== $1 ===${NC}\n"
}

success() {
    echo -e "${GREEN}$1${NC}"
}

fail() {
    echo -e "${RED}$1${NC}"
    exit 1
}

smoke_stream_logs_enabled() {
    [ "${SMOKE_STREAM_LOGS:-1}" = "1" ]
}

smoke_can_stream_logs() {
    smoke_stream_logs_enabled && command -v stdbuf >/dev/null 2>&1
}

smoke_env_file() {
    printf '%s\n' "${CRSBENCH_RUN_LOCAL_ENV_FILE:-$ROOT_DIR/.env}"
}

load_smoke_env_file() {
    local env_file
    local had_xtrace=0
    local load_rc=0
    env_file="$(smoke_env_file)"
    [ -f "$env_file" ] || return 0

    case "$-" in
        *x*)
            had_xtrace=1
            set +x
            ;;
    esac

    if ! eval "$(
        uv run python - "$env_file" <<'PY'
import os
import re
import shlex
import sys

from dotenv import dotenv_values

shell_identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

for key, value in dotenv_values(sys.argv[1]).items():
    if value is None or key in os.environ:
        continue
    if not shell_identifier.match(key):
        print(
            f"Skipping non-shell-safe key from smoke env file: {key}",
            file=sys.stderr,
        )
        continue
    print(f"export {key}={shlex.quote(value)}")
PY
    )"; then
        load_rc=$?
    fi

    if [ "$had_xtrace" -eq 1 ]; then
        set -x
    fi

    return "$load_rc"
}

create_smoke_stream_fifo_dir() {
    mktemp -d "${TMPDIR:-/tmp}/crsbench-smoke-stream-XXXXXX"
}

cleanup_smoke_stream_dir() {
    local stream_dir="$1"
    local fifo="$2"
    rm -f "$fifo"
    rmdir "$stream_dir" >/dev/null 2>&1 || true
}

run_smoke_logged_command() {
    local logfile="$1"
    local label="$2"
    local stream_dir fifo logger_pid cmd_rc
    shift 2

    : > "$logfile"
    if smoke_can_stream_logs; then
        stream_dir="$(create_smoke_stream_fifo_dir)"
        fifo="$stream_dir/stream"
        mkfifo "$fifo"
        (stdbuf -oL tee -a "$logfile" <"$fifo" | stdbuf -oL sed "s/^/[${label}] /") &
        logger_pid=$!
        if stdbuf -oL -eL "$@" >"$fifo" 2>&1; then
            cmd_rc=0
        else
            cmd_rc=$?
        fi
        wait "$logger_pid"
        cleanup_smoke_stream_dir "$stream_dir" "$fifo"
        return "$cmd_rc"
    else
        if "$@" >"$logfile" 2>&1; then
            return 0
        fi
        return $?
    fi
}

SMOKE_BG_PID=""
SMOKE_BG_LOGGER_PID=""
SMOKE_BG_STREAM_DIR=""
SMOKE_BG_STREAM_FIFO=""

cleanup_smoke_bg_logging() {
    if [ -n "$SMOKE_BG_LOGGER_PID" ]; then
        wait "$SMOKE_BG_LOGGER_PID"
    fi
    if [ -n "$SMOKE_BG_STREAM_FIFO" ]; then
        rm -f "$SMOKE_BG_STREAM_FIFO"
    fi
    if [ -n "$SMOKE_BG_STREAM_DIR" ]; then
        rmdir "$SMOKE_BG_STREAM_DIR" >/dev/null 2>&1 || true
    fi
    SMOKE_BG_LOGGER_PID=""
    SMOKE_BG_STREAM_FIFO=""
    SMOKE_BG_STREAM_DIR=""
}

start_smoke_logged_command_bg() {
    local logfile="$1"
    local label="$2"
    local stream_dir fifo
    shift 2

    : > "$logfile"
    SMOKE_BG_LOGGER_PID=""
    SMOKE_BG_STREAM_DIR=""
    SMOKE_BG_STREAM_FIFO=""
    if smoke_can_stream_logs; then
        stream_dir="$(create_smoke_stream_fifo_dir)"
        fifo="$stream_dir/stream"
        mkfifo "$fifo"
        (stdbuf -oL tee -a "$logfile" <"$fifo" | stdbuf -oL sed "s/^/[${label}] /") &
        SMOKE_BG_LOGGER_PID=$!
        SMOKE_BG_STREAM_DIR="$stream_dir"
        SMOKE_BG_STREAM_FIFO="$fifo"
        stdbuf -oL -eL "$@" >"$fifo" 2>&1 </dev/null &
    else
        "$@" >"$logfile" 2>&1 </dev/null &
    fi
    SMOKE_BG_PID=$!
}

SMOKE_VALKEY_CONTAINER=""
SMOKE_VALKEY_VOLUME=""
SMOKE_REDIS_PORT=""
SMOKE_ORIG_REDIS_HOST=""
SMOKE_HAD_ORIG_REDIS_HOST=0

cleanup_stale_temp_valkey() {
    local stale_containers stale_volumes
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi

    stale_containers=$(docker ps -a --filter "name=crsbench-smoke-valkey-" --format '{{.Names}}' 2>/dev/null || true)
    if [ -n "$stale_containers" ]; then
        while IFS= read -r container; do
            [ -z "$container" ] && continue
            docker rm -f "$container" >/dev/null 2>&1 || true
        done <<< "$stale_containers"
    fi

    stale_volumes=$(docker volume ls --filter "name=crsbench_smoke_valkey_" --format '{{.Name}}' 2>/dev/null || true)
    if [ -n "$stale_volumes" ]; then
        while IFS= read -r volume; do
            [ -z "$volume" ] && continue
            docker volume rm "$volume" >/dev/null 2>&1 || true
        done <<< "$stale_volumes"
    fi
}

start_temp_valkey() {
    local attempt port name volume
    cleanup_stale_temp_valkey
    if [ "${SMOKE_HAD_ORIG_REDIS_HOST}" -eq 0 ] && [ -n "${CRSBENCH_REDIS_HOST:-}" ]; then
        SMOKE_ORIG_REDIS_HOST="${CRSBENCH_REDIS_HOST}"
        SMOKE_HAD_ORIG_REDIS_HOST=1
    fi

    for attempt in $(seq 1 20); do
        port=$(python3 - <<'PY'
import random
print(random.randint(20000, 50000))
PY
)
        name="crsbench-smoke-valkey-${port}-$$"
        volume="crsbench_smoke_valkey_${port}_$$"

        if docker run -d \
            --name "$name" \
            -p "127.0.0.1:${port}:6379" \
            -v "${volume}:/data" \
            valkey/valkey:8.0-alpine \
            valkey-server --appendonly yes >/dev/null 2>&1; then
            SMOKE_VALKEY_CONTAINER="$name"
            SMOKE_VALKEY_VOLUME="$volume"
            SMOKE_REDIS_PORT="$port"
            local ready=0
            for _ in $(seq 1 20); do
                if docker exec "$SMOKE_VALKEY_CONTAINER" valkey-cli ping >/dev/null 2>&1; then
                    ready=1
                    break
                fi
                sleep 0.2
            done
            if [ "$ready" -ne 1 ]; then
                docker rm -f "$SMOKE_VALKEY_CONTAINER" >/dev/null 2>&1 || true
                docker volume rm "$SMOKE_VALKEY_VOLUME" >/dev/null 2>&1 || true
                SMOKE_VALKEY_CONTAINER=""
                SMOKE_VALKEY_VOLUME=""
                SMOKE_REDIS_PORT=""
                continue
            fi
            export CRSBENCH_REDIS_HOST="localhost:${SMOKE_REDIS_PORT}"
            echo "[smoke] started temporary Valkey: container=${SMOKE_VALKEY_CONTAINER} host=${CRSBENCH_REDIS_HOST}"
            return 0
        fi
    done

    fail "Failed to start temporary Valkey on a random port after 20 attempts"
}

cleanup_temp_valkey() {
    if [ -n "$SMOKE_VALKEY_CONTAINER" ]; then
        docker rm -f "$SMOKE_VALKEY_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [ -n "$SMOKE_VALKEY_VOLUME" ]; then
        docker volume rm "$SMOKE_VALKEY_VOLUME" >/dev/null 2>&1 || true
    fi
    if [ "${SMOKE_HAD_ORIG_REDIS_HOST}" -eq 1 ]; then
        export CRSBENCH_REDIS_HOST="${SMOKE_ORIG_REDIS_HOST}"
    else
        unset CRSBENCH_REDIS_HOST
    fi
    SMOKE_VALKEY_CONTAINER=""
    SMOKE_VALKEY_VOLUME=""
    SMOKE_REDIS_PORT=""
    SMOKE_ORIG_REDIS_HOST=""
    SMOKE_HAD_ORIG_REDIS_HOST=0
}

cleanup_path() {
    local path="$1"
    [ -z "$path" ] && return 0
    [ ! -e "$path" ] && return 0

    # Best-effort local cleanup first.
    rm -rf "$path" 2>/dev/null && return 0

    # Fallback for root-owned artifacts created by Docker during trials.
    if command -v docker >/dev/null 2>&1; then
        docker run --rm -v "$path:/cleanup-path" ubuntu:24.04 \
            bash -lc 'rm -rf /cleanup-path/* /cleanup-path/.[!.]* /cleanup-path/..?*' \
            >/dev/null 2>&1 || true
    fi

    rm -rf "$path" 2>/dev/null || true
}

smoke_config_for_suite() {
    local suite="$1"
    case "$suite" in
        bugfinding)
            echo "${SMOKE_BUGFINDING_CONFIG:-$SMOKE_BUGFINDING_CONFIG_DEFAULT}"
            ;;
        bugfixing)
            echo "${SMOKE_BUGFIXING_CONFIG:-$SMOKE_BUGFIXING_CONFIG_DEFAULT}"
            ;;
        *)
            return 1
            ;;
    esac
}

smoke_cpuset_for_suite() {
    local suite="$1"
    local normalized env_name default_cpuset
    normalized=$(echo "$suite" | tr '[:lower:]-' '[:upper:]_')
    env_name="SMOKE_CPUSET_${normalized}"
    case "$suite" in
        bugfinding) default_cpuset="0-23" ;;
        bugfixing) default_cpuset="24-47" ;;
        *) default_cpuset="" ;;
    esac

    echo "${!env_name:-$default_cpuset}"
}

smoke_skip_cpuset_for_suite() {
    local suite="$1"
    local normalized env_name
    normalized=$(echo "$suite" | tr '[:lower:]-' '[:upper:]_')
    env_name="SMOKE_SKIP_CPUSET_${normalized}"
    echo "${!env_name:-}"
}

smoke_skip_verification_for_suite() {
    local suite="$1"
    local normalized env_name
    normalized=$(echo "$suite" | tr '[:lower:]-' '[:upper:]_')
    env_name="SMOKE_SKIP_VERIFICATION_${normalized}"
    echo "${!env_name:-0}"
}

# Stage 1: Basic checks
run_checks() {
    run_stage "Stage 1: Basic Checks"

    echo "Running typecheck..."
    just typecheck || fail "Typecheck failed"
    success "Typecheck passed"

    echo "Running lint..."
    just lint || fail "Lint failed"
    success "Lint passed"

    echo "Running format check..."
    just format-check || fail "Format check failed"
    success "Format check passed"

    echo "Running benchmark path policy check..."
    uv run python scripts/ci-tests/check_benchmark_paths.py || fail "Benchmark path policy check failed"
    success "Benchmark path policy check passed"

    echo "Running unit tests (excluding integration)..."
    uv run pytest tests/ -v -n auto -m "not integration" || fail "Tests failed"
    success "Unit tests passed"

    success "Stage 1 completed!"
}

# Stage 2a: Format validation
run_format() {
    run_stage "Stage 2a: Format Validation"

    for benchmark in \
        sanity-mock-c-delta-01 \
        sanity-mock-java-delta-01; do
        # TODO: add HuggingFace download for non-sanity benchmarks
        # afc-xz-full-01 \
        # atlanta-json-java-full-01 \
        # afc-libxml2-full-01 \
        # afc-apache-commons-compress-delta-01; do
        uv run crsbench benchmark ci format "$benchmark" || fail "Format validation failed for $benchmark"
    done

    success "Stage 2a completed!"
}

# Stage 2b: Sanity checks (mock-c + mock-java)
run_sanity() {
    run_stage "Stage 2b: Sanity Checks (mock-c + mock-java)"

    for benchmark in sanity-mock-c-delta-01 sanity-mock-java-delta-01; do
        echo -e "\n${YELLOW}--- $benchmark ---${NC}"
        uv run crsbench benchmark ci all "$benchmark" \
            --force-rebuild || fail "All checks failed for $benchmark"
        success "$benchmark passed"
    done

    success "Stage 2b completed!"
}

# Stage 2c: Sanity checks (real projects)
# TODO: add HuggingFace download for non-sanity benchmarks
# run_sanity_real() {
#     run_stage "Stage 2c: Sanity Checks (real projects)"
#
#     for benchmark in afc-xz-full-01 atlanta-json-java-full-01; do
#         echo -e "\n${YELLOW}--- $benchmark ---${NC}"
#         uv run crsbench benchmark ci all "$benchmark" \
#             --force-rebuild || fail "All checks failed for $benchmark"
#         success "$benchmark passed"
#     done
#
#     success "Stage 2c completed!"
# }

# Integration tests (local only)
# TODO: add HuggingFace download for non-sanity benchmarks
# run_integration() {
#     run_stage "Integration Tests (Real Projects)"
#
#     for benchmark in afc-libxml2-full-01 afc-apache-commons-compress-delta-01; do
#         echo -e "\n${YELLOW}--- $benchmark ---${NC}"
#         uv run crsbench benchmark ci all "$benchmark" \
#             --force-rebuild || fail "All checks failed for $benchmark"
#         success "$benchmark passed"
#     done
#
#     success "Integration tests completed!"
# }

# Stage 3: E2E Bug Finding
run_e2e() {
    run_stage "Stage 3: E2E Bug Finding"

    EXPERIMENT_DIR=$(mktemp -d)
    REPORT_DIR=$(mktemp -d)

    # Generate config with temp directories
    sed \
        -e "s|PLACEHOLDER_EXPERIMENT|$EXPERIMENT_DIR|" \
        -e "s|PLACEHOLDER_REPORT|$REPORT_DIR|" \
        scripts/ci-tests/local-e2e-bugfinding.yaml > /tmp/pov-e2e-config.yaml

    echo "Validating generated E2E config..."
    uv run python - "/tmp/pov-e2e-config.yaml" <<'PY' || fail "E2E config validation failed"
from pathlib import Path
import sys
from crsbench.run_experiment import load_experiment_config

load_experiment_config(Path(sys.argv[1]))
print(f"validated: {sys.argv[1]}")
PY

    echo "Running E2E experiment (max 30 minutes, early stop enabled)..."
    uv run crsbench run --experiment-config /tmp/pov-e2e-config.yaml || fail "E2E experiment failed"

    echo "Verifying POV results..."
    # Expected CPVs per benchmark/harness
    declare -A EXPECTED
    EXPECTED["sanity-mock-c-delta-01/fuzz_process_input_header"]="cpv_0"
    EXPECTED["sanity-mock-c-delta-01/fuzz_parse_buffer_section"]="cpv_1"
    EXPECTED["sanity-mock-java-delta-01/OssFuzz1"]="cpv_0"

    FAILED=0
    for key in "${!EXPECTED[@]}"; do
        bench=$(echo "$key" | cut -d'/' -f1)
        harness=$(echo "$key" | cut -d'/' -f2)
        expected_cpv="${EXPECTED[$key]}"

        POV_STORE=$(find "$EXPERIMENT_DIR" -path "*/$bench/$harness/*/trial-*/povs/pov_store.json" | head -1)

        if [ -z "$POV_STORE" ]; then
            echo -e "${RED}FAIL: No pov_store.json for $bench/$harness${NC}"
            FAILED=1
            continue
        fi

        if python3 -c "
import json, sys
with open('$POV_STORE') as f:
    data = json.load(f)
cpvs = set()
for pov in data.get('povs', {}).values():
    for cpv in pov.get('cpv_matched', []):
        cpvs.add(cpv)
if '$expected_cpv' not in cpvs:
    sys.exit(1)
"; then
            echo -e "${GREEN}PASS: $bench/$harness found $expected_cpv${NC}"
        else
            echo -e "${RED}FAIL: $bench/$harness missing $expected_cpv${NC}"
            FAILED=1
        fi
    done

    # Cleanup (best-effort; oss-crs may create root-owned files)
    cleanup_path "$EXPERIMENT_DIR"
    cleanup_path "$REPORT_DIR"
    rm -f /tmp/pov-e2e-config.yaml || true

    if [ "$FAILED" -ne 0 ]; then
        fail "E2E FAILED: Not all answer POVs were found"
    fi

    success "E2E PASSED: All answer POVs found!"
}

# Stage 4: smoke checks for default regression CRSs
# Smoke stages run against an isolated temporary Valkey instance on a random port.
# The container/volume are cleaned up automatically after each smoke run.
run_litellm_preflight() {
    local require_tracking="${1:-0}"
    echo "Running LiteLLM preflight (health + runtime auth)..."
    if [ "$require_tracking" = "1" ]; then
        uv run python scripts/ci-tests/litellm_preflight.py --mode external --require-tracking || fail "LiteLLM preflight failed"
    else
        uv run python scripts/ci-tests/litellm_preflight.py --mode external || fail "LiteLLM preflight failed"
    fi
    success "LiteLLM preflight passed"
}

suite_requires_litellm() {
    local suite="$1"
    local config_path
    config_path="$(smoke_config_for_suite "$suite")" || return 1
    uv run python - "$config_path" <<'PY'
from pathlib import Path
import sys
import yaml

def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

data = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
runtime = data.get("runtime", {})
litellm = runtime.get("litellm", {}) if isinstance(runtime, dict) else {}
mode = litellm.get("mode")
if mode is None:
    mode = runtime.get("litellm_mode") if isinstance(runtime, dict) else None
if mode is None:
    mode = data.get("litellm_mode", "external")
skip_litellm = as_bool(
    litellm["skip"]
    if isinstance(litellm, dict) and "skip" in litellm
    else (
        runtime["skip_litellm"]
        if isinstance(runtime, dict) and "skip_litellm" in runtime
        else data.get("skip_litellm", False)
    )
)
requires = (mode not in (None, "null")) and not skip_litellm
print("1" if requires else "0")
PY
}

suite_requires_litellm_tracking() {
    local suite="$1"
    local config_path
    config_path="$(smoke_config_for_suite "$suite")" || return 1
    uv run python - "$config_path" <<'PY'
from pathlib import Path
import sys
import yaml

def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

data = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
runtime = data.get("runtime", {})
litellm = runtime.get("litellm", {}) if isinstance(runtime, dict) else {}
mode = litellm.get("mode")
if mode is None:
    mode = runtime.get("litellm_mode") if isinstance(runtime, dict) else None
if mode is None:
    mode = data.get("litellm_mode", "external")
skip_litellm = as_bool(
    litellm["skip"]
    if isinstance(litellm, dict) and "skip" in litellm
    else (
        runtime["skip_litellm"]
        if isinstance(runtime, dict) and "skip_litellm" in runtime
        else data.get("skip_litellm", False)
    )
)
tracking_enabled = as_bool(
    litellm.get(
        "tracking_enabled",
        runtime.get("llm_tracking_enabled", data.get("llm_tracking_enabled", True))
        if isinstance(runtime, dict)
        else data.get("llm_tracking_enabled", True),
    )
)
requires = (mode not in (None, "null")) and (not skip_litellm) and tracking_enabled
print("1" if requires else "0")
PY
}

validate_smoke_selection_config() {
    local bugfinding_cfg bugfixing_cfg
    bugfinding_cfg="$(smoke_config_for_suite bugfinding)" || fail "Unknown smoke suite bugfinding"
    bugfixing_cfg="$(smoke_config_for_suite bugfixing)" || fail "Unknown smoke suite bugfixing"
    [ -f "$bugfinding_cfg" ] || fail "Missing smoke config: $bugfinding_cfg"
    [ -f "$bugfixing_cfg" ] || fail "Missing smoke config: $bugfixing_cfg"
    uv run python - "$bugfinding_cfg" "$bugfixing_cfg" <<'PY'
from pathlib import Path
import sys
import yaml

for p in sys.argv[1:]:
    data = yaml.safe_load(Path(p).read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"invalid YAML root in {p}: expected mapping")
    print(f"[smoke] validated config syntax: {p}")
PY
}

render_smoke_config() {
    local suite="$1"
    local base_config="$2"
    local out_config="$3"
    local exp_dir="$4"
    local report_dir="$5"
    local redis_host="$6"
    local skip_verification="${7:-0}"

    uv run python - "$suite" "$base_config" "$out_config" "$exp_dir" "$report_dir" "$redis_host" "$skip_verification" <<'PY'
from pathlib import Path
from datetime import datetime
import sys
import yaml

suite, base_path, out_path, exp_dir, report_dir, redis_host, skip_verification = sys.argv[1:]
data = yaml.safe_load(Path(base_path).read_text()) or {}
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

experiment = data.get("experiment")
if isinstance(experiment, dict):
    base_name = experiment.get("name", f"smoke-{suite}")
    experiment["name"] = f"{base_name}-{timestamp}-{suite}"
elif isinstance(experiment, str):
    data["experiment"] = f"{experiment}-{timestamp}-{suite}"
else:
    data["experiment"] = {"name": f"smoke-{suite}-{timestamp}"}

runtime = data.setdefault("runtime", {})
runtime["redis_host"] = redis_host
if str(skip_verification).strip() == "1":
    runtime["skip_verification"] = True

storage = data.setdefault("storage", {})
storage["experiment_filestore"] = exp_dir
storage["report_filestore"] = report_dir

Path(out_path).write_text(yaml.safe_dump(data, sort_keys=False))
exp_name = data["experiment"]["name"] if isinstance(data["experiment"], dict) else data["experiment"]
print(exp_name)
PY
}

stop_worker_process() {
    local pid="$1"
    local label="$2"
    local signal timeout

    if ! kill -0 "$pid" >/dev/null 2>&1; then
        wait "$pid" >/dev/null 2>&1 || true
        if [ "$pid" = "$SMOKE_BG_PID" ]; then
            cleanup_smoke_bg_logging
        fi
        return 0
    fi

    for signal in INT TERM KILL; do
        case "$signal" in
            INT|TERM) timeout=20 ;;
            KILL) timeout=5 ;;
            *) timeout=5 ;;
        esac

        kill "-$signal" "$pid" >/dev/null 2>&1 || true
        local elapsed=0
        while [ "$elapsed" -lt "$timeout" ]; do
            if ! kill -0 "$pid" >/dev/null 2>&1; then
                wait "$pid" >/dev/null 2>&1 || true
                if [ "$pid" = "$SMOKE_BG_PID" ]; then
                    cleanup_smoke_bg_logging
                fi
                return 0
            fi
            sleep 1
            elapsed=$((elapsed + 1))
        done
    done

    echo "[smoke] warning: $label (pid=$pid) did not terminate cleanly"
    wait "$pid" >/dev/null 2>&1 || true
    if [ "$pid" = "$SMOKE_BG_PID" ]; then
        cleanup_smoke_bg_logging
    fi
    return 1
}

run_smoke_suite_config() {
    local suite="$1"
    local stage_name="$2"
    local base_config workspace config_path exp_dir report_dir
    local worker_log evaluator_log run_log post_verify_log worker_pid evaluator_pid rc cpuset skip_cpuset skip_verification

    run_stage "$stage_name"
    base_config="$(smoke_config_for_suite "$suite")" || fail "Unknown smoke suite: $suite"
    [ -f "$base_config" ] || fail "Missing smoke config: $base_config"

    workspace=$(mktemp -d "/tmp/crsbench-smoke-${suite}-XXXXXX")
    config_path="$workspace/experiment-config.yaml"
    exp_dir="$workspace/experiment-data"
    report_dir="$workspace/report-data"
    mkdir -p "$exp_dir" "$report_dir"

    skip_verification="$(smoke_skip_verification_for_suite "$suite")"
    render_smoke_config "$suite" "$base_config" "$config_path" "$exp_dir" "$report_dir" "${CRSBENCH_REDIS_HOST}" "$skip_verification" >/dev/null \
        || fail "Failed to render smoke config for $suite"

    worker_log="$workspace/worker.log"
    evaluator_log="$workspace/evaluator.log"
    run_log="$workspace/run.log"
    post_verify_log="$workspace/post-verify.log"

    local worker_cmd=(uv run crsbench worker --experiment-config "$config_path" --continuous)
    if [ "${SMOKE_NO_CPUSET:-1}" != "1" ]; then
        cpuset="$(smoke_cpuset_for_suite "$suite")"
        skip_cpuset="$(smoke_skip_cpuset_for_suite "$suite")"
        if [ -n "$cpuset" ]; then
            worker_cmd+=(--cpuset "$cpuset")
        fi
        if [ -n "$skip_cpuset" ]; then
            worker_cmd+=(--skip-cpuset "$skip_cpuset")
        fi
    fi

    start_smoke_logged_command_bg "$worker_log" "smoke:${suite}:worker" "${worker_cmd[@]}"
    worker_pid="$SMOKE_BG_PID"

    # Start evaluator — builds variant images and consumes build/verify
    # queues.  Bugfinding: async POV verification + early stop.
    # Bugfixing: distributed patch build + verify.
    local evaluator_cmd=(uv run crsbench evaluator --experiment-config "$config_path" --idle-timeout 0)
    start_smoke_logged_command_bg "$evaluator_log" "smoke:${suite}:evaluator" "${evaluator_cmd[@]}"
    evaluator_pid="$SMOKE_BG_PID"

    sleep 3
    if ! kill -0 "$worker_pid" >/dev/null 2>&1; then
        stop_worker_process "$evaluator_pid" "evaluator[$suite]" || true
        cleanup_smoke_bg_logging
        echo "=== worker log (tail) ==="
        tail -n 120 "$worker_log" || true
        fail "Smoke worker failed to start for suite=$suite"
    fi

    rc=0
    run_smoke_logged_command "$run_log" "smoke:${suite}:run" \
        uv run crsbench run --experiment-config "$config_path" --distributed --queue-mode fresh || rc=$?

    if ! stop_worker_process "$worker_pid" "worker[$suite]"; then
        echo "=== worker log (tail) ==="
        tail -n 200 "$worker_log" || true
        stop_worker_process "$evaluator_pid" "evaluator[$suite]" || true
        fail "Smoke worker did not terminate cleanly for suite=$suite"
    fi

    # Stop evaluator after run completes.  With --idle-timeout 0 it stays
    # alive until killed.  The worker is already stopped so no new verify
    # jobs will arrive; stop_worker_process gives 20s grace for SIGINT to
    # let the evaluator drain remaining verify work.
    if ! stop_worker_process "$evaluator_pid" "evaluator[$suite]"; then
        echo "=== evaluator log (tail) ==="
        tail -n 200 "$evaluator_log" || true
        echo "[smoke] evaluator[$suite] did not terminate cleanly"
    fi

    if [ "$rc" -ne 0 ]; then
        echo "=== run log (tail) ==="
        tail -n 200 "$run_log" || true
        echo "=== worker log (tail) ==="
        tail -n 200 "$worker_log" || true
        echo "=== evaluator log (tail) ==="
        tail -n 200 "$evaluator_log" || true
        fail "Smoke run failed for suite=$suite"
    fi

    if [ "$skip_verification" = "1" ]; then
        echo "[smoke] skipping post-run verification for suite=$suite"
    else
        rc=0
        run_smoke_logged_command "$post_verify_log" "smoke:${suite}:verify" \
            uv run python -m crsbench.benchmark_ci.smoke_post_verify \
                --suite "$suite" \
                --experiment-dir "$exp_dir" \
                --benchmarks-root "$ROOT_DIR/benchmarks" || rc=$?

        if [ "$rc" -ne 0 ]; then
            echo "=== post-verify log (tail) ==="
            tail -n 200 "$post_verify_log" || true
            echo "=== run log (tail) ==="
            tail -n 200 "$run_log" || true
            fail "Smoke post-verification failed for suite=$suite"
        fi
    fi

    if [ "${SMOKE_KEEP_WORKSPACE:-0}" = "1" ]; then
        echo "[smoke] kept workspace: $workspace"
    else
        cleanup_path "$workspace"
    fi
    success "Smoke $suite passed"
}

run_smoke_bugfinding() {
    local bugfind_requires_litellm bugfind_requires_tracking
    load_smoke_env_file
    bugfind_requires_litellm="$(suite_requires_litellm bugfinding)" || fail "Failed to inspect bugfinding smoke LiteLLM requirements"
    bugfind_requires_tracking="$(suite_requires_litellm_tracking bugfinding)" || fail "Failed to inspect bugfinding smoke LiteLLM tracking requirements"
    if [ "$bugfind_requires_litellm" = "1" ]; then
        run_litellm_preflight "$bugfind_requires_tracking"
    fi
    start_temp_valkey
    trap cleanup_temp_valkey EXIT
    run_smoke_suite_config bugfinding "Stage 4a: Smoke Bugfinding (config-first)"
    trap - EXIT
    cleanup_temp_valkey
}

run_smoke_bugfixing() {
    local bugfix_requires_litellm bugfix_requires_tracking
    load_smoke_env_file
    bugfix_requires_litellm="$(suite_requires_litellm bugfixing)" || fail "Failed to inspect bugfixing smoke LiteLLM requirements"
    bugfix_requires_tracking="$(suite_requires_litellm_tracking bugfixing)" || fail "Failed to inspect bugfixing smoke LiteLLM tracking requirements"
    if [ "$bugfix_requires_litellm" = "1" ]; then
        run_litellm_preflight "$bugfix_requires_tracking"
    fi
    start_temp_valkey
    trap cleanup_temp_valkey EXIT
    run_smoke_suite_config bugfixing "Stage 4b: Smoke Bugfixing (config-first)"
    trap - EXIT
    cleanup_temp_valkey
}

run_smoke_parallel() {
    run_stage "Stage 4: Parallel Smoke (config-first)"
    local bugfind_requires_litellm bugfind_requires_tracking
    local bugfix_requires_litellm bugfix_requires_tracking
    local parallel_requires_litellm parallel_requires_tracking
    load_smoke_env_file
    bugfind_requires_litellm="$(suite_requires_litellm bugfinding)" || fail "Failed to inspect bugfinding smoke LiteLLM requirements"
    bugfind_requires_tracking="$(suite_requires_litellm_tracking bugfinding)" || fail "Failed to inspect bugfinding smoke LiteLLM tracking requirements"
    bugfix_requires_litellm="$(suite_requires_litellm bugfixing)" || fail "Failed to inspect bugfixing smoke LiteLLM requirements"
    bugfix_requires_tracking="$(suite_requires_litellm_tracking bugfixing)" || fail "Failed to inspect bugfixing smoke LiteLLM tracking requirements"
    parallel_requires_litellm=0
    parallel_requires_tracking=0
    if [ "$bugfind_requires_litellm" = "1" ] || [ "$bugfix_requires_litellm" = "1" ]; then
        parallel_requires_litellm=1
    fi
    if [ "$bugfind_requires_tracking" = "1" ] || [ "$bugfix_requires_tracking" = "1" ]; then
        parallel_requires_tracking=1
    fi
    if [ "$parallel_requires_litellm" = "1" ]; then
        run_litellm_preflight "$parallel_requires_tracking"
    fi

    start_temp_valkey
    trap cleanup_temp_valkey EXIT

    local pids=()
    local suites=(bugfinding bugfixing)
    local suite
    for suite in "${suites[@]}"; do
        run_smoke_suite_config "$suite" "Smoke $suite" &
        pids+=("$!")
    done

    local rc=0
    for pid in "${pids[@]}"; do
        wait "$pid" || rc=1
    done

    trap - EXIT
    cleanup_temp_valkey
    [ "$rc" -eq 0 ] || fail "Parallel smoke failed"
    success "Parallel smoke passed"
}

# Main
main() {
    local stage=${1:-default}

    echo -e "${YELLOW}CRSBench CI Local Runner${NC}"
    echo "Running from: $ROOT_DIR"

    case $stage in
        checks)
            run_checks
            ;;
        format)
            run_format
            ;;
        sanity)
            run_sanity
            ;;
        # TODO: re-enable after adding HuggingFace download
        # sanity-real)
        #     run_sanity_real
        #     ;;
        # integration)
        #     run_integration
        #     ;;
        e2e)
            run_e2e
            ;;
        smoke)
            run_smoke_parallel
            ;;
        smoke-validate-config)
            validate_smoke_selection_config
            ;;
        smoke-bugfinding)
            run_smoke_bugfinding
            ;;
        smoke-bugfixing)
            run_smoke_bugfixing
            ;;
        default)
            # Default: matches CI pipeline (checks → format → mock → smoke)
            run_checks
            run_format
            run_sanity
            # TODO: re-enable after adding HuggingFace download
            # run_sanity_real
            run_smoke_parallel
            ;;
        all)
            run_checks
            run_format
            run_sanity
            # TODO: re-enable after adding HuggingFace download
            # run_sanity_real
            # run_integration
            run_smoke_parallel
            ;;
        *)
            echo "Usage: $0 [checks|format|sanity|e2e|smoke|smoke-validate-config|smoke-bugfinding|smoke-bugfixing]"
            echo ""
            echo "  (default)    Run checks + format + sanity-mock + smoke (matches CI)"
            echo "  checks       Stage 1: typecheck, lint, format, unit tests"
            echo "  format       Stage 2a: format validation (sanity benchmarks)"
            echo "  sanity       Stage 2b: all checks (mock-c + mock-java)"
            echo "  e2e          Optional: bug finding E2E (longer/deeper)"
            echo "  smoke        Stage 4: parallel smoke checks (bugfinding + bugfixing)"
            echo "  smoke-validate-config  Validate config-first smoke config files"
            echo "  smoke-bugfinding  Smoke check for bugfinding sanity config"
            echo "  smoke-bugfixing   Smoke check for bugfixing sanity config"
            exit 1
            ;;
    esac

    echo -e "\n${GREEN}Completed successfully!${NC}"
}

if [ "${CRSBENCH_RUN_LOCAL_SOURCE_ONLY:-0}" != "1" ]; then
    main "$@"
fi

#!/bin/bash
# Run CI checks locally
#
# CI runs: checks → format → sanity-mock → e2e
#
# Usage:
#   ./ci-tests/run-local.sh              # Run full CI pipeline (checks + format + mock + smoke)
#   ./ci-tests/run-local.sh checks       # Stage 1: typecheck, lint, format, unit tests
#   ./ci-tests/run-local.sh format       # Stage 2a: format validation (sanity benchmarks)
#   ./ci-tests/run-local.sh sanity       # Stage 2b: all checks (mock-c + mock-java)
#   ./ci-tests/run-local.sh e2e          # Stage 3: bug finding E2E
#   ./ci-tests/run-local.sh smoke        # Stage 4: parallel smoke (bugfinding + bugfixing)
#   ./ci-tests/run-local.sh smoke-bugfinding
#   ./ci-tests/run-local.sh smoke-bugfixing
#
# TODO: re-enable after adding HuggingFace download to CI
#   ./ci-tests/run-local.sh all          # Run all stages including integration
#   ./ci-tests/run-local.sh sanity-real  # Stage 2c: all checks (afc-xz + json-java)
#   ./ci-tests/run-local.sh integration  # Local only: real projects (libxml2, commons-compress)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

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

SMOKE_VALKEY_CONTAINER=""
SMOKE_VALKEY_VOLUME=""
SMOKE_REDIS_PORT=""
SMOKE_ORIG_REDIS_HOST=""
SMOKE_HAD_ORIG_REDIS_HOST=0

start_temp_valkey() {
    local attempt port name volume
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
        ci-tests/pov-e2e-test.yaml > /tmp/pov-e2e-config.yaml

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
run_smoke_bugfinding() {
    run_stage "Stage 4a: Smoke Bugfinding (atlantis-multilang-given_fuzzer)"
    start_temp_valkey
    trap cleanup_temp_valkey EXIT
    uv run python ci-tests/smoke_runner.py \
        --suite bugfinding \
        --worker-cores 16 \
        --keep-workspace || fail "Smoke bugfinding failed"
    trap - EXIT
    cleanup_temp_valkey
    success "Smoke bugfinding passed"
}

run_smoke_bugfixing() {
    run_stage "Stage 4b: Smoke Bugfixing (crs-claude-code)"
    start_temp_valkey
    trap cleanup_temp_valkey EXIT
    uv run python ci-tests/smoke_runner.py \
        --suite bugfixing \
        --worker-cores 16 \
        --keep-workspace || fail "Smoke bugfixing failed"
    trap - EXIT
    cleanup_temp_valkey
    success "Smoke bugfixing passed"
}

run_smoke_parallel() {
    run_stage "Stage 4: Parallel Smoke (bugfinding + bugfixing)"

    # Run in parallel with disjoint cpusets by default.
    # Override with env vars if your machine has a different layout.
    local bugfinding_cpuset=${SMOKE_CPUSET_BUGFINDING:-0-23}
    local bugfixing_cpuset=${SMOKE_CPUSET_BUGFIXING:-24-47}
    local smoke_run_root
    smoke_run_root=$(mktemp -d /tmp/crsbench-smoke-parallel-XXXXXX)
    start_temp_valkey
    trap cleanup_temp_valkey EXIT

    uv run python ci-tests/smoke_runner.py \
        --suite bugfinding \
        --worker-cpuset "$bugfinding_cpuset" \
        --result-root "$smoke_run_root" \
        --keep-workspace &
    pid1=$!

    uv run python ci-tests/smoke_runner.py \
        --suite bugfixing \
        --worker-cpuset "$bugfixing_cpuset" \
        --result-root "$smoke_run_root" \
        --keep-workspace &
    pid2=$!

    wait "$pid1" || fail "Parallel smoke bugfinding failed"
    wait "$pid2" || fail "Parallel smoke bugfixing failed"

    echo -e "\n${YELLOW}--- Parallel Smoke Summary ---${NC}"
    local summaries
    summaries=$(find "$smoke_run_root" -name summary.json -type f | sort || true)
    if [ -z "$summaries" ]; then
        fail "Parallel smoke completed but no summary.json files found under $smoke_run_root"
    fi

    while IFS= read -r summary; do
        [ -z "$summary" ] && continue
        python3 - "$summary" <<'PY'
import json, sys
from pathlib import Path

summary_path = Path(sys.argv[1])
data = json.loads(summary_path.read_text())
suite = data.get("suite", "unknown")
status = data.get("status", "unknown")
workspace = data.get("workspace", "unknown")
successes = data.get("successes", 0)
total_trials = data.get("total_trials", 0)
patch_files = data.get("patch_files", 0)
pov_files = data.get("pov_files", 0)
print(f"[{suite}] status={status} successes={successes}/{total_trials} patch_files={patch_files} pov_files={pov_files}")
print(f"[{suite}] workspace={workspace}")
PY
    done <<< "$summaries"

    echo "[smoke] summary root: $smoke_run_root"
    trap - EXIT
    cleanup_temp_valkey
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
            echo "Usage: $0 [checks|format|sanity|e2e|smoke|smoke-bugfinding|smoke-bugfixing]"
            echo ""
            echo "  (default)    Run checks + format + sanity-mock + smoke (matches CI)"
            echo "  checks       Stage 1: typecheck, lint, format, unit tests"
            echo "  format       Stage 2a: format validation (sanity benchmarks)"
            echo "  sanity       Stage 2b: all checks (mock-c + mock-java)"
            echo "  e2e          Optional: bug finding E2E (longer/deeper)"
            echo "  smoke        Stage 4: parallel smoke checks (bugfinding + bugfixing)"
            echo "  smoke-bugfinding  Smoke check for atlantis-multilang-given_fuzzer"
            echo "  smoke-bugfixing   Smoke check for crs-claude-code"
            exit 1
            ;;
    esac

    echo -e "\n${GREEN}Completed successfully!${NC}"
}

main "$@"

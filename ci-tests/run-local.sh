#!/bin/bash
# Run CI checks locally
#
# Usage:
#   ./ci-tests/run-local.sh              # Run Stage 1-2 (auto checks)
#   ./ci-tests/run-local.sh all          # Run all stages
#   ./ci-tests/run-local.sh checks       # Stage 1: typecheck, lint, format, test
#   ./ci-tests/run-local.sh sanity       # Stage 2: mock-c verify + patch-verify
#   ./ci-tests/run-local.sh integration  # Stage 3: other projects
#   ./ci-tests/run-local.sh e2e          # Stage 4: bug finding E2E

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

    echo "Running unit tests..."
    uv run pytest tests/ -v -n auto || fail "Unit tests failed"
    success "Unit tests passed"

    success "Stage 1 completed!"
}

# Stage 2: Sanity check (mock-c)
run_sanity() {
    run_stage "Stage 2: Sanity Check (mock-c)"

    local benchmark="sanity-mock-c-delta-01"

    echo "Verifying $benchmark..."
    uv run crsbench verify "benchmarks/$benchmark" \
        --force-rebuild \
        --output "verify-results.json" \
        --format json || fail "Verify failed"

    python3 ci-tests/check_ci_results.py verify \
        "benchmarks/$benchmark" \
        "verify-results.json" || fail "CPV check failed"
    success "Verify passed"

    echo "Patch-verifying $benchmark..."
    uv run crsbench patch-verify "benchmarks/$benchmark" \
        --force-rebuild \
        --output "patch-verify-results.json" \
        --format json || fail "Patch-verify failed"

    python3 ci-tests/check_ci_results.py patch-verify \
        "patch-verify-results.json" || fail "Patch check failed"
    success "Patch-verify passed"

    rm -f verify-results.json patch-verify-results.json
    success "Stage 2 completed!"
}

# Stage 3: Integration tests
run_integration() {
    run_stage "Stage 3: Integration Tests"

    local benchmarks=(
        "sanity-mock-java-delta-01"
        "afc-libxml2-full-01"
        "afc-apache-commons-compress-delta-01"
    )

    for benchmark in "${benchmarks[@]}"; do
        echo "Testing $benchmark..."

        uv run crsbench verify "benchmarks/$benchmark" \
            --force-rebuild \
            --output "verify-results.json" \
            --format json || fail "Verify failed for $benchmark"

        python3 ci-tests/check_ci_results.py verify \
            "benchmarks/$benchmark" \
            "verify-results.json" || fail "CPV check failed for $benchmark"

        uv run crsbench patch-verify "benchmarks/$benchmark" \
            --force-rebuild \
            --output "patch-verify-results.json" \
            --format json || fail "Patch-verify failed for $benchmark"

        python3 ci-tests/check_ci_results.py patch-verify \
            "patch-verify-results.json" || fail "Patch check failed for $benchmark"

        success "$benchmark passed"
        rm -f verify-results.json patch-verify-results.json
    done

    success "Stage 3 completed!"
}

# Stage 4: Bug finding E2E
run_e2e() {
    run_stage "Stage 4: Bug Finding E2E"

    echo "Running E2E experiment..."
    uv run crsbench --experiment-config experiment-configs/coverage-e2e-test.yaml || fail "E2E failed"

    local result_dir="/tmp/crsbench/experiments/coverage-e2e-test"
    if [ ! -d "$result_dir" ]; then
        fail "Experiment directory not found: $result_dir"
    fi

    local trial_count
    trial_count=$(find "$result_dir" -type d -name "trial-*" | wc -l)
    echo "Trial directories found: $trial_count"

    if [ "$trial_count" -eq 0 ]; then
        fail "No trial directories found"
    fi

    success "Stage 4 completed!"
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
        sanity)
            run_sanity
            ;;
        integration)
            run_integration
            ;;
        e2e)
            run_e2e
            ;;
        default)
            # Default: run Stage 1-2 (auto checks)
            run_checks
            run_sanity
            ;;
        all)
            run_checks
            run_sanity
            run_integration
            run_e2e
            ;;
        *)
            echo "Usage: $0 [checks|sanity|integration|e2e|all]"
            echo ""
            echo "  (default)    Run Stage 1-2 (auto checks)"
            echo "  checks       Stage 1: typecheck, lint, format, test"
            echo "  sanity       Stage 2: mock-c verify + patch-verify"
            echo "  integration  Stage 3: other projects"
            echo "  e2e          Stage 4: bug finding E2E"
            echo "  all          Run all stages"
            exit 1
            ;;
    esac

    echo -e "\n${GREEN}Completed successfully!${NC}"
}

main "$@"

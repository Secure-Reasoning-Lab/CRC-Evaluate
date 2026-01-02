#!/bin/bash
# Run CI checks locally
#
# CI runs: checks → sanity (mock-c, mock-java in parallel) → e2e
# Local adds: integration (libxml2, apache-commons-compress)
#
# Usage:
#   ./ci-tests/run-local.sh              # Run checks + sanity (matches CI)
#   ./ci-tests/run-local.sh all          # Run all stages including integration
#   ./ci-tests/run-local.sh checks       # Stage 1: typecheck, lint, format, unit tests
#   ./ci-tests/run-local.sh sanity       # Stage 2: verify, patch-verify, coverage (mock-c + mock-java)
#   ./ci-tests/run-local.sh integration  # Local only: real projects (libxml2, commons-compress)
#   ./ci-tests/run-local.sh e2e          # Stage 3: bug finding E2E

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

# Run verify → patch-verify → coverage for a single benchmark
run_benchmark() {
    local benchmark=$1
    echo -e "\n${YELLOW}--- $benchmark ---${NC}"

    # Verify
    echo "Verifying $benchmark..."
    uv run crsbench verify "benchmarks/$benchmark" \
        --force-rebuild \
        --output "verify-$benchmark.json" \
        --format json || fail "Verify failed for $benchmark"

    uv run python3 ci-tests/check_ci_results.py verify \
        "benchmarks/$benchmark" \
        "verify-$benchmark.json" || fail "Verify check failed for $benchmark"
    success "Verify passed"
    rm -f "verify-$benchmark.json"

    # Patch-verify
    echo "Patch-verifying $benchmark..."
    uv run crsbench patch-verify "benchmarks/$benchmark" \
        --force-rebuild \
        --output "patch-verify-$benchmark.json" \
        --format json || fail "Patch-verify failed for $benchmark"

    uv run python3 ci-tests/check_ci_results.py patch-verify \
        "patch-verify-$benchmark.json" || fail "Patch-verify check failed for $benchmark"
    success "Patch-verify passed"
    rm -f "patch-verify-$benchmark.json"

    # Coverage
    echo "Coverage $benchmark..."
    local corpus_dir
    corpus_dir=$(mktemp -d)
    head -c 64 /dev/urandom > "$corpus_dir/seed_input"

    uv run crsbench coverage "benchmarks/$benchmark" \
        --corpus-dir "$corpus_dir" \
        --force-rebuild \
        --output "coverage-$benchmark.json" \
        --format json || { rm -rf "$corpus_dir"; fail "Coverage failed for $benchmark"; }

    rm -rf "$corpus_dir"
    success "Coverage passed"
    rm -f "coverage-$benchmark.json"

    success "$benchmark completed!"
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

    echo "Running all tests..."
    uv run pytest tests/ -v -n auto || fail "Tests failed"
    success "All tests passed"

    success "Stage 1 completed!"
}

# Stage 2: Sanity checks (mock-c + mock-java)
run_sanity() {
    run_stage "Stage 2: Sanity Checks (mock-c + mock-java)"

    run_benchmark "sanity-mock-c-delta-01"
    run_benchmark "sanity-mock-java-delta-01"

    success "Stage 2 completed!"
}

# Stage 3: Integration tests (real projects - local only, not in CI)
run_integration() {
    run_stage "Stage 3: Integration Tests (Real Projects)"

    run_benchmark "afc-libxml2-full-01"
    run_benchmark "afc-apache-commons-compress-delta-01"

    success "Stage 3 completed!"
}

# Stage 3 (CI) / Stage 4 (local): Bug finding E2E
run_e2e() {
    run_stage "E2E Bug Finding"

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

    success "E2E completed!"
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
            echo "  (default)    Run checks + sanity (matches CI)"
            echo "  checks       Stage 1: typecheck, lint, format, unit tests"
            echo "  sanity       Stage 2: verify, patch-verify, coverage (mock-c + mock-java)"
            echo "  integration  Local only: real projects (libxml2, commons-compress)"
            echo "  e2e          Stage 3: bug finding E2E"
            echo "  all          Run all stages including integration"
            exit 1
            ;;
    esac

    echo -e "\n${GREEN}Completed successfully!${NC}"
}

main "$@"

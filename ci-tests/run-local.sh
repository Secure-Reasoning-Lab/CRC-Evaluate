#!/bin/bash
# Run CI checks locally
#
# CI runs: checks → format → sanity-mock → sanity-real → e2e
# Local adds: integration (libxml2, apache-commons-compress)
#
# Usage:
#   ./ci-tests/run-local.sh              # Run full CI pipeline (checks + format + mock + real + e2e)
#   ./ci-tests/run-local.sh all          # Run all stages including integration
#   ./ci-tests/run-local.sh checks       # Stage 1: typecheck, lint, format, unit tests
#   ./ci-tests/run-local.sh format       # Stage 2a: format validation (CI + integration benchmarks)
#   ./ci-tests/run-local.sh sanity       # Stage 2b: all checks (mock-c + mock-java)
#   ./ci-tests/run-local.sh sanity-real  # Stage 2c: all checks (afc-xz + json-java)
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
        sanity-mock-java-delta-01 \
        afc-xz-full-01 \
        atlanta-json-java-full-01 \
        afc-libxml2-full-01 \
        afc-apache-commons-compress-delta-01; do
        uv run crsbench ci format "$benchmark" || fail "Format validation failed for $benchmark"
    done

    success "Stage 2a completed!"
}

# Stage 2b: Sanity checks (mock-c + mock-java)
run_sanity() {
    run_stage "Stage 2b: Sanity Checks (mock-c + mock-java)"

    for benchmark in sanity-mock-c-delta-01 sanity-mock-java-delta-01; do
        echo -e "\n${YELLOW}--- $benchmark ---${NC}"
        uv run crsbench ci all "$benchmark" \
            --no-inc-build \
            --force-rebuild || fail "All checks failed for $benchmark"
        success "$benchmark passed"
    done

    success "Stage 2b completed!"
}

# Stage 2c: Sanity checks (real projects)
run_sanity_real() {
    run_stage "Stage 2c: Sanity Checks (real projects)"

    for benchmark in afc-xz-full-01 atlanta-json-java-full-01; do
        echo -e "\n${YELLOW}--- $benchmark ---${NC}"
        uv run crsbench ci all "$benchmark" \
            --force-rebuild || fail "All checks failed for $benchmark"
        success "$benchmark passed"
    done

    success "Stage 2c completed!"
}

# Integration tests (local only)
run_integration() {
    run_stage "Integration Tests (Real Projects)"

    for benchmark in afc-libxml2-full-01 afc-apache-commons-compress-delta-01; do
        echo -e "\n${YELLOW}--- $benchmark ---${NC}"
        uv run crsbench ci all "$benchmark" \
            --force-rebuild || fail "All checks failed for $benchmark"
        success "$benchmark passed"
    done

    success "Integration tests completed!"
}

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

    # Cleanup
    rm -rf "$EXPERIMENT_DIR" "$REPORT_DIR" /tmp/pov-e2e-config.yaml

    if [ "$FAILED" -ne 0 ]; then
        fail "E2E FAILED: Not all answer POVs were found"
    fi

    success "E2E PASSED: All answer POVs found!"
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
        sanity-real)
            run_sanity_real
            ;;
        integration)
            run_integration
            ;;
        e2e)
            run_e2e
            ;;
        default)
            # Default: matches CI pipeline (checks → format → mock → real → e2e)
            run_checks
            run_format
            run_sanity
            run_sanity_real
            run_e2e
            ;;
        all)
            run_checks
            run_format
            run_sanity
            run_sanity_real
            run_integration
            run_e2e
            ;;
        *)
            echo "Usage: $0 [checks|format|sanity|sanity-real|integration|e2e|all]"
            echo ""
            echo "  (default)    Run checks + format + sanity-mock + sanity-real + e2e (matches CI)"
            echo "  checks       Stage 1: typecheck, lint, format, unit tests"
            echo "  format       Stage 2a: format validation (CI + integration benchmarks)"
            echo "  sanity       Stage 2b: all checks (mock-c + mock-java)"
            echo "  sanity-real  Stage 2c: all checks (afc-xz + json-java)"
            echo "  integration  Local only: real projects (libxml2, commons-compress)"
            echo "  e2e          Stage 3: bug finding E2E"
            echo "  all          Run all stages"
            exit 1
            ;;
    esac

    echo -e "\n${GREEN}Completed successfully!${NC}"
}

main "$@"

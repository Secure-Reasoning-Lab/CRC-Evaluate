#!/bin/bash

# This performs the CP-specific tests. It may be replaced with a script
# or binary for a different interpreter. The name MUST NOT change.

set -e
set -o pipefail

warn() {
    echo "$*" >&2
}

die() {
    warn "$*"
    exit 1
}

$CC $CFLAGS -c $SRC/tests/fuzz-shim.c -o $SRC/tests/fuzz-shim.o
LIB_FUZZING_ENGINE=$SRC/tests/fuzz-shim.o $SRC/build.sh

[[ -x ${OUT}/fuzz_process_input_header ]] || die "FAILURE: missing binary: ${OUT}/fuzz_process_input_header"
[[ -x ${OUT}/fuzz_parse_buffer_section ]] || die "FAILURE: missing binary: ${OUT}/fuzz_parse_buffer_section"
[[ -f ${SRC}/tests/test1.blob ]] || die "FAILURE: missing test file: ${SRC}/tests/test1.blob"
[[ -f ${SRC}/tests/test2.blob ]] || die "FAILURE: missing test file: ${SRC}/tests/test2.blob"

${OUT}/fuzz_process_input_header ${SRC}/tests/test1.blob 2>&1 | grep -q "Executed" || \
    { warn "FAILURE: test1 failed"; exit 2; }

${OUT}/fuzz_parse_buffer_section ${SRC}/tests/test2.blob 2>&1 | grep -q "Executed" || \
    { warn "FAILURE: test2 failed"; exit 2; }

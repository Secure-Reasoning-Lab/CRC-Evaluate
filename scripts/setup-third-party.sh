#!/bin/bash
# One-time setup: fetch official oss-fuzz via sparse checkout.
#
# This replaces the old in-repo ./oss-fuzz dependency with a managed
# third_party checkout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
THIRD_PARTY="$REPO_ROOT/third_party"
PATCH_DIR="$THIRD_PARTY/patches"

OSS_FUZZ_REPO="https://github.com/google/oss-fuzz.git"
OSS_FUZZ_COMMIT="1f5c75e09c7b8b98a0e4f21859602a89d41602c2"
OSS_FUZZ_DIR="$THIRD_PARTY/oss-fuzz"
OSS_FUZZ_HELPER_PATCHES=(
    "$PATCH_DIR/oss-fuzz-helper-cgroup.patch"
    "$PATCH_DIR/oss-fuzz-helper-build-image.patch"
)

apply_one_helper_patch() {
    local patch_file="$1"
    if [ ! -f "$patch_file" ]; then
        echo "No local helper patch found at $patch_file (skipping)"
        return 0
    fi

    # Already applied?
    if git -C "$OSS_FUZZ_DIR" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
        echo "Local helper patch already applied: $patch_file"
        return 0
    fi

    # Applicable now?
    if git -C "$OSS_FUZZ_DIR" apply --check "$patch_file" >/dev/null 2>&1; then
        echo "Applying local helper patch: $patch_file"
        git -C "$OSS_FUZZ_DIR" apply "$patch_file"
        return 0
    fi

    echo "Failed to apply local helper patch: $patch_file"
    echo "The managed oss-fuzz checkout may have diverged from expected commit."
    return 1
}

apply_helper_patches() {
    local patch_file
    for patch_file in "${OSS_FUZZ_HELPER_PATCHES[@]}"; do
        apply_one_helper_patch "$patch_file"
    done
}

if [ -d "$OSS_FUZZ_DIR/.git" ]; then
    echo "oss-fuzz already checked out at $OSS_FUZZ_DIR"
    apply_helper_patches
    echo "To re-fetch, remove the directory first: rm -rf $OSS_FUZZ_DIR"
    exit $?
fi

echo "Fetching official oss-fuzz via sparse checkout..."
mkdir -p "$THIRD_PARTY"
git clone --filter=blob:none --sparse "$OSS_FUZZ_REPO" "$OSS_FUZZ_DIR"
cd "$OSS_FUZZ_DIR"
git checkout "$OSS_FUZZ_COMMIT"
# Keep checkout minimal for CRSBench runtime:
# - infra/: required for official helper.py and dependencies
# - selected top-level docs/licenses requested for provenance
git sparse-checkout set --no-cone \
    "/infra/" \
    "/AGENTS.md" \
    "/CITATION.cff" \
    "/CONTRIBUTING.md" \
    "/LICENSE" \
    "/README.md"

# CRSBench creates per-benchmark symlinks under oss-fuzz/projects/.
# Prepare the parent directory without checking out the upstream projects tree.
mkdir -p projects

apply_helper_patches

echo "Done. official oss-fuzz checked out to $OSS_FUZZ_DIR"

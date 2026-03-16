#!/bin/bash
# One-time setup: fetch managed third_party dependencies used by CRSBench.
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

ATLANTIS_REPO="https://github.com/Team-Atlanta/atlantis-multilang-given_fuzzer.git"
ATLANTIS_REF="1.0.0"
ATLANTIS_DIR="$THIRD_PARTY/atlantis-multilang-given_fuzzer"

usage() {
    cat <<'EOF'
Usage: scripts/setup-third-party.sh [--oss-fuzz-only] [--atlantis-only]

Bootstraps the managed third_party checkouts CRSBench expects:
- third_party/oss-fuzz
- third_party/atlantis-multilang-given_fuzzer

The Atlantis checkout is pinned to tag 1.0.0 to match the published GHCR
prepare/runtime images. Pull or validate those images separately with:
  uv run crsbench prepare --coverage
EOF
}

BOOTSTRAP_OSS_FUZZ=1
BOOTSTRAP_ATLANTIS=1

while [ $# -gt 0 ]; do
    case "$1" in
        --oss-fuzz-only)
            BOOTSTRAP_ATLANTIS=0
            ;;
        --atlantis-only)
            BOOTSTRAP_OSS_FUZZ=0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

apply_one_helper_patch() {
    local patch_file="$1"
    if [ ! -f "$patch_file" ]; then
        echo "No local helper patch found at $patch_file (skipping)"
        return 0
    fi

    if git -C "$OSS_FUZZ_DIR" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
        echo "Local helper patch already applied: $patch_file"
        return 0
    fi

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

bootstrap_oss_fuzz() {
    if [ -d "$OSS_FUZZ_DIR/.git" ]; then
        echo "oss-fuzz already checked out at $OSS_FUZZ_DIR"
        apply_helper_patches
        return 0
    fi

    echo "Fetching official oss-fuzz via sparse checkout..."
    mkdir -p "$THIRD_PARTY"
    git clone --filter=blob:none --sparse "$OSS_FUZZ_REPO" "$OSS_FUZZ_DIR"
    git -C "$OSS_FUZZ_DIR" checkout "$OSS_FUZZ_COMMIT"
    git -C "$OSS_FUZZ_DIR" sparse-checkout set --no-cone \
        "/infra/" \
        "/AGENTS.md" \
        "/CITATION.cff" \
        "/CONTRIBUTING.md" \
        "/LICENSE" \
        "/README.md"
    mkdir -p "$OSS_FUZZ_DIR/projects"
    apply_helper_patches
    echo "Done. official oss-fuzz checked out to $OSS_FUZZ_DIR"
}

bootstrap_atlantis() {
    if [ -d "$ATLANTIS_DIR/.git" ]; then
        local current_ref
        current_ref="$(git -C "$ATLANTIS_DIR" describe --tags --exact-match 2>/dev/null || true)"
        if [ "$current_ref" != "$ATLANTIS_REF" ]; then
            echo "ERROR: Atlantis checkout at $ATLANTIS_DIR is not pinned to tag $ATLANTIS_REF"
            echo "  Current exact tag: ${current_ref:-<none>}"
            echo "  Remove the checkout and rerun this script to reprovision the pinned release."
            return 1
        fi
        if [ -n "$(git -C "$ATLANTIS_DIR" status --porcelain --untracked-files=no)" ]; then
            echo "ERROR: Atlantis checkout at $ATLANTIS_DIR has tracked modifications."
            echo "  Reset or remove the checkout before rerunning this script."
            return 1
        fi
        echo "Atlantis given_fuzzer already checked out at $ATLANTIS_DIR"
        return 0
    fi

    echo "Fetching Team Atlanta atlantis-multilang-given_fuzzer at tag $ATLANTIS_REF..."
    mkdir -p "$THIRD_PARTY"
    git clone --depth 1 --branch "$ATLANTIS_REF" "$ATLANTIS_REPO" "$ATLANTIS_DIR"
    echo "Done. Atlantis checkout created at $ATLANTIS_DIR"
}

if [ "$BOOTSTRAP_OSS_FUZZ" -eq 1 ]; then
    bootstrap_oss_fuzz
fi

if [ "$BOOTSTRAP_ATLANTIS" -eq 1 ]; then
    bootstrap_atlantis
fi

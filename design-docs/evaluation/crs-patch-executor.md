# CRS Patch Executor Design

## Purpose

Implements the patch generation CRS executor using the `oss-patch-crs` (oss-bugfix-crs) CLI interface.

## Implementation Overview

### CLI Interface

**Build command:**
```bash
oss-bugfix-crs build <crs> <project> \
  --oss-fuzz <path> \
  --project-path <benchmark-dir> \
  --source-path <pre-cloned-source> \
  --registry <registry-dir> \
  [--gitcache]
```

**Run command:**
```bash
oss-bugfix-crs run <crs> <project> \
  --harness <harness-name> \
  --povs <povs-dir> \
  --out <output-dir> \
  [--hints <hints-dir>]
  # LiteLLM via env vars: LITELLM_API_BASE, LITELLM_API_KEY
```

### Key Differences from Bug Finding Executor

1. **Different CLI**: Uses `oss-bugfix-crs` instead of `oss-bugfind-crs`
2. **No --build-dir**: Build state is stored in Docker images
3. **POVs required**: Must provide `--povs` directory with POV files
4. **LiteLLM via env**: Uses environment variables instead of CLI args
5. **Output parameter**: Uses `--out` instead of `--output`

### Implementation Details

#### Constructor
- Add `registry_dir: Path` parameter for CRS registry
- Store as instance variable

#### Build Phase
- Call `ensure_project_repository()` to get pre-cloned source
- Pass `--registry` to build command
- Add debug logging for command

#### Run Phase
- Prepare POVs directory (required) - fail if missing
- Use `--out` parameter for output
- Pass LiteLLM config via environment variables
- Add debug logging for command

#### Pre-build Support
- Add `build_crs()` public method for pre-building before snapshot
- Follows same pattern as bug finding executor

## File Changes

### crsbench/evaluation/crs_patch_executor.py
- Line ~33: Add `registry_dir` to `__init__`
- Line ~87-110: Add `build_crs()` method
- Line ~273-283: Update build command with `--registry` and logging
- Line ~108-131: Update run command with `--out`, env vars, and logging

## Testing

- Verify `--out` parameter works correctly
- Test LiteLLM via environment variables
- Confirm POVs directory validation
- Test pre-build functionality

## References

- OSS-Fuzz CRS Interface: `docs/ossfuzz-crs-interface.md`
- CRS Executors Design: `design-docs/evaluation/crs-executors.md`
- Source implementation: `oss-crs/bug_fixing/src/__main__.py`

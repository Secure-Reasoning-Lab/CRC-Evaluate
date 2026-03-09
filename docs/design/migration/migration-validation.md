# Migration Validation Design

Audience: contributors changing migration validation guarantees or issue taxonomy.
Scope: validation contract for Team-Atlanta to RFC migration output, not CLI workflow details.

## Overview

This document describes validation functionality for the migration module to verify that Team-Atlanta to RFC format conversion was successful by comparing source and target directories.

## Goals

- Compare source (Team-Atlanta) with target (RFC format) to verify migration
- Verify all files from source exist in target
- Check file sizes match between source and target
- Provide actionable error messages for failed validations

## Validation Scope

### Source-Target Comparison

Compare source Team-Atlanta project with migrated RFC benchmark:

**Root Files:**
- `build.sh`, `Dockerfile`, `project.yaml`
- All `*.options` files

**Artifact Files (per harness/cpv):**
- Source: `.aixcc/patches/{harness}/{cpv}.diff` → Target: `.aixcc/{harness}/{cpv}/patches/patch_0.diff`
- Source: `.aixcc/povs/{harness}/{cpv}` → Target: `.aixcc/{harness}/{cpv}/blobs/pov_0.blob`
- Source: `.aixcc/crash_logs/{harness}/{cpv}.log` → Target: `.aixcc/{harness}/{cpv}/logs/pov_0.log`

## Implementation Contract

The concrete validator implementation lives in
`crsbench/migration/migration_validator.py`. This document defines the
validation guarantees and issue taxonomy, not the exact callable signatures.

#### Validation Codes

| Code | Level | Description |
|------|-------|-------------|
| `SOURCE_NOT_FOUND` | error | Source directory not found |
| `CONFIG_NOT_FOUND` | error | Source config.yaml not found |
| `META_PARSE_ERROR` | error | config.yaml parse error |
| `MISSING_ROOT_FILE` | error | Root file not migrated |
| `MISSING_PATCH` | error | Patch file not migrated |
| `MISSING_POV_BLOB` | error | POV blob not migrated |
| `MISSING_CRASH_LOG` | error | Crash log not migrated |
| `HASH_MISMATCH` | warning | File hash (SHA256) differs |
| `SOURCE_FILE_NOT_FOUND` | warning | Source file missing |

## Operational Boundary

Runnable migration-validation commands belong in contributor-facing migration
workflows. This design page records what the validator must compare and what
issue codes it must emit.

## Error Handling

- **Missing source**: Return immediately with error
- **Missing config.yaml**: Return immediately with error
- **Missing target files**: Collect all issues before returning
- **Hash mismatch**: Report as warning (uses SHA256)

## Validation Coverage

Regression coverage should verify:
- missing source/config preconditions fail fast
- valid migrations pass without issues
- missing root or artifact files are reported
- content mismatches are surfaced with the expected severity
- missing source-side comparison material degrades to warnings where intended

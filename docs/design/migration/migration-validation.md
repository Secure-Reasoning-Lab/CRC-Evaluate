# Migration Validation Design

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

## Implementation

### Module: `crsbench/migration/migration_validator.py`

#### Main Function

```python
def validate_source_target(source_dir: Path, target_dir: Path) -> MigrationValidationResult:
    """
    Validate migration by comparing source with target.

    Args:
        source_dir: Path to Team-Atlanta source project
        target_dir: Path to migrated RFC benchmark

    Returns:
        MigrationValidationResult with validation status and issues
    """
```

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

## Usage

```bash
# Validate all projects
python -m crsbench.migration.migration_validator \
  --source-dir /path/to/team-atlanta/projects \
  --target-dir /path/to/benchmarks

# Validate specific projects
python -m crsbench.migration.migration_validator \
  --source-dir /path/to/team-atlanta/projects \
  --target-dir /path/to/benchmarks \
  --projects curl-delta-04,libxml2-delta-03

# Verbose output
python -m crsbench.migration.migration_validator \
  --source-dir /path/to/team-atlanta/projects \
  --target-dir /path/to/benchmarks \
  --verbose
```

### Example Workflow

```bash
# 1. Run migration
python crsbench/migration/atlanta_to_rfc.py \
  --source-dir /path/to/team-atlanta/projects \
  --target-dir /path/to/benchmarks

# 2. Validate migrated projects
python -m crsbench.migration.migration_validator \
  --source-dir /path/to/team-atlanta/projects \
  --target-dir /path/to/benchmarks
```

## Error Handling

- **Missing source**: Return immediately with error
- **Missing config.yaml**: Return immediately with error
- **Missing target files**: Collect all issues before returning
- **Hash mismatch**: Report as warning (uses SHA256)

## Testing

Test cases in `tests/test_migration_validator.py`:

1. Missing source directory returns error
2. Missing source config returns error
3. Valid migration passes validation
4. Missing root files detected
5. Missing patch/blob/log files detected
6. File size mismatches detected
7. Missing source files generate warnings

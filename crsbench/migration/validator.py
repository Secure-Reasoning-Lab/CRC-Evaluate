"""
Migration validation module for verifying Team-Atlanta to RFC format conversion.

This module validates that migration was successful by comparing source
(Team-Atlanta) with target (RFC format) directories.
"""

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from crsbench.utils import log_summary
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _compute_file_hash(file_path: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with file_path.open("rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


# =============================================================================
# Validation Codes
# =============================================================================


class ValidationCodes:
    """Validation error/warning codes."""

    # Error codes
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    META_PARSE_ERROR = "META_PARSE_ERROR"
    MISSING_ROOT_FILE = "MISSING_ROOT_FILE"
    MISSING_PATCH = "MISSING_PATCH"
    MISSING_POV_BLOB = "MISSING_POV_BLOB"
    MISSING_CRASH_LOG = "MISSING_CRASH_LOG"

    # Warning codes
    HASH_MISMATCH = "HASH_MISMATCH"
    SOURCE_FILE_NOT_FOUND = "SOURCE_FILE_NOT_FOUND"


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class ValidationIssue:
    """A single validation issue (error or warning)."""

    level: str  # "error" or "warning"
    code: str  # Validation code from ValidationCodes
    message: str  # Human-readable description
    path: Optional[str] = None  # Related file/directory path


@dataclass
class MigrationValidationResult:
    """Result of migration validation."""

    is_valid: bool = True
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[ValidationIssue]:
        """Get all error-level issues."""
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        """Get all warning-level issues."""
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def error_count(self) -> int:
        """Get count of errors."""
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        """Get count of warnings."""
        return len(self.warnings)

    def summary(self) -> str:
        """Generate human-readable summary."""
        status = "VALID" if self.is_valid else "INVALID"
        lines = [
            f"Validation Result: {status}",
            f"  Errors: {self.error_count}",
            f"  Warnings: {self.warning_count}",
        ]

        if self.errors:
            lines.append("\nErrors:")
            for issue in self.errors:
                path_info = f" ({issue.path})" if issue.path else ""
                lines.append(f"  - [{issue.code}] {issue.message}{path_info}")

        if self.warnings:
            lines.append("\nWarnings:")
            for issue in self.warnings:
                path_info = f" ({issue.path})" if issue.path else ""
                lines.append(f"  - [{issue.code}] {issue.message}{path_info}")

        return "\n".join(lines)


# =============================================================================
# Source-Target Comparison Validation
# =============================================================================


def validate_source_target(
    source_dir: Path, target_dir: Path
) -> MigrationValidationResult:
    """
    Validate migration by comparing source (Team-Atlanta) with target (RFC format).

    Args:
        source_dir: Path to Team-Atlanta source project directory
        target_dir: Path to migrated RFC benchmark directory

    Returns:
        MigrationValidationResult with validation status and issues
    """
    result = MigrationValidationResult()

    # Check source exists
    if not source_dir.exists():
        result.issues.append(
            ValidationIssue(
                level="error",
                code=ValidationCodes.SOURCE_NOT_FOUND,
                message=f"Source directory not found: {source_dir}",
                path=str(source_dir),
            )
        )
        result.is_valid = False
        return result

    # Check source config.yaml exists
    source_config = source_dir / ".aixcc" / "config.yaml"
    if not source_config.exists():
        result.issues.append(
            ValidationIssue(
                level="error",
                code=ValidationCodes.CONFIG_NOT_FOUND,
                message="Source config.yaml not found",
                path=str(source_config),
            )
        )
        result.is_valid = False
        return result

    # Load source config to get harness/cpv info
    try:
        with source_config.open("r") as f:
            config_data = yaml.safe_load(f)
    except Exception as e:
        result.issues.append(
            ValidationIssue(
                level="error",
                code=ValidationCodes.META_PARSE_ERROR,
                message=f"Failed to parse source config.yaml: {e}",
                path=str(source_config),
            )
        )
        result.is_valid = False
        return result

    # Validate root files
    result.issues.extend(_validate_root_files(source_dir, target_dir))

    # Validate artifacts for each harness/cpv
    harness_files = config_data.get("harness_files", [])
    for harness in harness_files:
        harness_name = harness.get("name", "")
        cpvs = harness.get("cpvs", [])

        for cpv in cpvs:
            cpv_name = cpv.get("name", "")
            result.issues.extend(
                _validate_cpv_artifacts(source_dir, target_dir, harness_name, cpv_name)
            )

    # Determine overall validity
    result.is_valid = len(result.errors) == 0

    return result


def _validate_root_files(source_dir: Path, target_dir: Path) -> List[ValidationIssue]:
    """
    Validate that root files from source exist in target.

    Args:
        source_dir: Source project directory
        target_dir: Target benchmark directory

    Returns:
        List of validation issues
    """
    issues = []

    # List of important root files to check
    important_files = ["build.sh", "Dockerfile", "project.yaml"]

    for filename in important_files:
        source_file = source_dir / filename
        target_file = target_dir / filename

        if source_file.exists():
            if not target_file.exists():
                issues.append(
                    ValidationIssue(
                        level="error",
                        code=ValidationCodes.MISSING_ROOT_FILE,
                        message=f"Root file {filename} not migrated",
                        path=str(target_file),
                    )
                )
            elif _compute_file_hash(source_file) != _compute_file_hash(target_file):
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code=ValidationCodes.HASH_MISMATCH,
                        message=f"Hash mismatch for {filename}",
                        path=str(target_file),
                    )
                )

    # Check .options files
    for options_file in source_dir.glob("*.options"):
        target_options = target_dir / options_file.name
        if not target_options.exists():
            issues.append(
                ValidationIssue(
                    level="error",
                    code=ValidationCodes.MISSING_ROOT_FILE,
                    message=f"Options file {options_file.name} not migrated",
                    path=str(target_options),
                )
            )

    return issues


def _validate_cpv_artifacts(
    source_dir: Path, target_dir: Path, harness_name: str, cpv_name: str
) -> List[ValidationIssue]:
    """
    Validate that CPV artifacts from source exist in target.

    Args:
        source_dir: Source project directory
        target_dir: Target benchmark directory
        harness_name: Harness name
        cpv_name: CPV name (e.g., cpv_0)

    Returns:
        List of validation issues
    """
    issues = []
    source_aixcc = source_dir / ".aixcc"
    target_cpv_dir = target_dir / ".aixcc" / harness_name / cpv_name

    # Check patch file
    source_patch = source_aixcc / "patches" / harness_name / f"{cpv_name}.diff"
    target_patch = target_cpv_dir / "patches" / "patch_0.diff"

    if source_patch.exists():
        if not target_patch.exists():
            issues.append(
                ValidationIssue(
                    level="error",
                    code=ValidationCodes.MISSING_PATCH,
                    message=f"Patch not migrated for {harness_name}/{cpv_name}",
                    path=str(target_patch),
                )
            )
        elif _compute_file_hash(source_patch) != _compute_file_hash(target_patch):
            issues.append(
                ValidationIssue(
                    level="warning",
                    code=ValidationCodes.HASH_MISMATCH,
                    message=f"Patch hash mismatch for {harness_name}/{cpv_name}",
                    path=str(target_patch),
                )
            )
    else:
        issues.append(
            ValidationIssue(
                level="warning",
                code=ValidationCodes.SOURCE_FILE_NOT_FOUND,
                message=f"Source patch not found for {harness_name}/{cpv_name}",
                path=str(source_patch),
            )
        )

    # Check POV blob
    source_pov = source_aixcc / "povs" / harness_name / cpv_name
    target_pov = target_cpv_dir / "blobs" / "pov_0.blob"

    if source_pov.exists():
        if not target_pov.exists():
            issues.append(
                ValidationIssue(
                    level="error",
                    code=ValidationCodes.MISSING_POV_BLOB,
                    message=f"POV blob not migrated for {harness_name}/{cpv_name}",
                    path=str(target_pov),
                )
            )
        elif _compute_file_hash(source_pov) != _compute_file_hash(target_pov):
            issues.append(
                ValidationIssue(
                    level="warning",
                    code=ValidationCodes.HASH_MISMATCH,
                    message=f"POV blob hash mismatch for {harness_name}/{cpv_name}",
                    path=str(target_pov),
                )
            )
    else:
        issues.append(
            ValidationIssue(
                level="warning",
                code=ValidationCodes.SOURCE_FILE_NOT_FOUND,
                message=f"Source POV blob not found for {harness_name}/{cpv_name}",
                path=str(source_pov),
            )
        )

    # Check crash log
    source_log = source_aixcc / "crash_logs" / harness_name / f"{cpv_name}.log"
    target_log = target_cpv_dir / "logs" / "pov_0.log"

    if source_log.exists():
        if not target_log.exists():
            issues.append(
                ValidationIssue(
                    level="error",
                    code=ValidationCodes.MISSING_CRASH_LOG,
                    message=f"Crash log not migrated for {harness_name}/{cpv_name}",
                    path=str(target_log),
                )
            )
        elif _compute_file_hash(source_log) != _compute_file_hash(target_log):
            issues.append(
                ValidationIssue(
                    level="warning",
                    code=ValidationCodes.HASH_MISMATCH,
                    message=f"Crash log hash mismatch for {harness_name}/{cpv_name}",
                    path=str(target_log),
                )
            )
    else:
        issues.append(
            ValidationIssue(
                level="warning",
                code=ValidationCodes.SOURCE_FILE_NOT_FOUND,
                message=f"Source crash log not found for {harness_name}/{cpv_name}",
                path=str(source_log),
            )
        )

    return issues


# =============================================================================
# CLI Support
# =============================================================================


def main():
    """CLI entry point for migration validation."""
    parser = argparse.ArgumentParser(
        description="Validate migration by comparing source and target directories"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Path to Team-Atlanta source projects directory (contains aixcc/)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        required=True,
        help="Path to migrated benchmarks directory",
    )
    parser.add_argument(
        "--projects",
        type=str,
        help="Comma-separated list of specific projects to validate",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    # Setup logging
    from crsbench.utils.logger import configure_logger

    log_level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=log_level)

    source_dir = args.source_dir
    target_dir = args.target_dir

    # Validate source directory
    if not source_dir.exists():
        logger.error(f"Source directory not found: {source_dir}")
        sys.exit(1)

    # Find source projects (Team-Atlanta structure: source_dir/aixcc/{language}/{project})
    aixcc_dir = source_dir / "aixcc"
    if not aixcc_dir.exists():
        logger.error(f"aixcc directory not found in source: {aixcc_dir}")
        sys.exit(1)

    # Parse specific projects if provided
    specific_projects = None
    if args.projects:
        specific_projects = {p.strip() for p in args.projects.split(",")}

    # Discover source projects
    source_projects = []
    allowed_languages = ["c", "java", "jvm"]

    for lang_dir in aixcc_dir.iterdir():
        if not lang_dir.is_dir() or lang_dir.name.lower() not in allowed_languages:
            continue

        for project_dir in lang_dir.iterdir():
            if not project_dir.is_dir():
                continue
            if not (project_dir / ".aixcc").exists():
                continue
            if specific_projects and project_dir.name not in specific_projects:
                continue
            source_projects.append(project_dir)

    if not source_projects:
        logger.error(f"No source projects found in {source_dir}")
        sys.exit(1)

    logger.info(f"Found {len(source_projects)} projects to validate")

    total = len(source_projects)
    valid = 0
    invalid = 0

    for source_project in sorted(source_projects):
        project_name = source_project.name
        target_project = target_dir / project_name

        logger.info(f"Validating: {project_name}")

        # Check if target exists
        if not target_project.exists():
            logger.error(f"  ✗ {project_name}: Target not found")
            invalid += 1
            continue

        # Run source-target comparison validation
        result = validate_source_target(source_project, target_project)

        if result.is_valid:
            valid += 1
            logger.info(f"  ✓ {project_name}: VALID")
        else:
            invalid += 1
            logger.error(f"  ✗ {project_name}: INVALID")

        # Show issues
        for issue in result.errors:
            logger.error(f"    [{issue.code}] {issue.message}")
        for issue in result.warnings:
            logger.warning(f"    [{issue.code}] {issue.message}")

    # Summary
    log_summary(
        "Validation",
        {"total": total, "valid": valid, "invalid": invalid},
        show_percentage=False,
    )

    sys.exit(0 if invalid == 0 else 1)


if __name__ == "__main__":
    main()

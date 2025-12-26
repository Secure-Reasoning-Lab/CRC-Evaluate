"""
Tests for migration validation module.

These tests verify that the migration validator correctly identifies
issues when comparing source and target directories.
"""

import tempfile
from pathlib import Path

import yaml
from crsbench.migration.migration_validator import (
    MigrationValidationResult,
    ValidationCodes,
    ValidationIssue,
    validate_source_target,
)


class TestValidationIssue:
    """Tests for ValidationIssue dataclass."""

    def test_create_error(self):
        """Test creating an error-level issue."""
        issue = ValidationIssue(
            level="error",
            code=ValidationCodes.SOURCE_NOT_FOUND,
            message="Source not found",
            path="/test/path",
        )
        assert issue.level == "error"
        assert issue.code == ValidationCodes.SOURCE_NOT_FOUND
        assert issue.message == "Source not found"
        assert issue.path == "/test/path"

    def test_create_warning(self):
        """Test creating a warning-level issue."""
        issue = ValidationIssue(
            level="warning", code=ValidationCodes.HASH_MISMATCH, message="Hash mismatch"
        )
        assert issue.level == "warning"
        assert issue.path is None


class TestMigrationValidationResult:
    """Tests for MigrationValidationResult dataclass."""

    def test_empty_result_is_valid(self):
        """Test that empty result is valid by default."""
        result = MigrationValidationResult()
        assert result.is_valid is True
        assert len(result.issues) == 0
        assert result.error_count == 0
        assert result.warning_count == 0

    def test_errors_property(self):
        """Test filtering errors from issues."""
        result = MigrationValidationResult(
            is_valid=False,
            issues=[
                ValidationIssue(level="error", code="E1", message="Error 1"),
                ValidationIssue(level="warning", code="W1", message="Warning 1"),
                ValidationIssue(level="error", code="E2", message="Error 2"),
            ],
        )
        assert result.error_count == 2
        assert result.warning_count == 1
        assert len(result.errors) == 2
        assert len(result.warnings) == 1

    def test_summary(self):
        """Test generating summary string."""
        result = MigrationValidationResult(
            is_valid=False,
            issues=[
                ValidationIssue(
                    level="error", code="E1", message="Error message", path="/path"
                ),
            ],
        )
        summary = result.summary()
        assert "INVALID" in summary
        assert "Errors: 1" in summary
        assert "E1" in summary
        assert "Error message" in summary


class TestValidateSourceTarget:
    """Tests for source-target comparison validation."""

    def test_missing_source_directory(self):
        """Test that missing source directory returns error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "nonexistent"
            target_dir = Path(tmpdir) / "target"
            target_dir.mkdir()

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is False
            assert any(
                e.code == ValidationCodes.SOURCE_NOT_FOUND for e in result.errors
            )

    def test_missing_source_config(self):
        """Test that missing source config.yaml returns error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"
            source_dir.mkdir()
            target_dir.mkdir()
            (source_dir / ".aixcc").mkdir()  # No config.yaml

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is False
            assert any(
                e.code == ValidationCodes.CONFIG_NOT_FOUND for e in result.errors
            )

    def test_valid_source_target(self):
        """Test that valid migration passes validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is True
            assert result.error_count == 0

    def test_missing_root_file(self):
        """Test that missing root file is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            # Remove build.sh from target
            (target_dir / "build.sh").unlink()

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is False
            assert any(
                e.code == ValidationCodes.MISSING_ROOT_FILE for e in result.errors
            )

    def test_missing_patch_file(self):
        """Test that missing patch file is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            # Remove patch from target
            patch_file = (
                target_dir
                / ".aixcc"
                / "test_harness"
                / "cpv_0"
                / "patches"
                / "patch_0.diff"
            )
            patch_file.unlink()

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is False
            assert any(e.code == ValidationCodes.MISSING_PATCH for e in result.errors)

    def test_missing_pov_blob(self):
        """Test that missing POV blob is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            # Remove POV blob from target
            blob_file = (
                target_dir
                / ".aixcc"
                / "test_harness"
                / "cpv_0"
                / "blobs"
                / "pov_0.blob"
            )
            blob_file.unlink()

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is False
            assert any(
                e.code == ValidationCodes.MISSING_POV_BLOB for e in result.errors
            )

    def test_missing_crash_log(self):
        """Test that missing crash log is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            # Remove crash log from target
            log_file = (
                target_dir / ".aixcc" / "test_harness" / "cpv_0" / "logs" / "pov_0.log"
            )
            log_file.unlink()

            result = validate_source_target(source_dir, target_dir)

            assert result.is_valid is False
            assert any(
                e.code == ValidationCodes.MISSING_CRASH_LOG for e in result.errors
            )

    def test_hash_mismatch(self):
        """Test that hash mismatch is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            # Modify target file to have different content
            patch_file = (
                target_dir
                / ".aixcc"
                / "test_harness"
                / "cpv_0"
                / "patches"
                / "patch_0.diff"
            )
            patch_file.write_text("different content!")

            result = validate_source_target(source_dir, target_dir)

            # Should have warning for hash mismatch
            assert any(w.code == ValidationCodes.HASH_MISMATCH for w in result.warnings)

    def test_source_file_not_found_warning(self):
        """Test that missing source files generate warnings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            target_dir = Path(tmpdir) / "target"

            _create_valid_source_structure(source_dir)
            _create_valid_target_from_source(source_dir, target_dir)

            # Remove source patch (target still has it)
            source_patch = (
                source_dir / ".aixcc" / "patches" / "test_harness" / "cpv_0.diff"
            )
            source_patch.unlink()

            result = validate_source_target(source_dir, target_dir)

            # Should have warning for missing source file
            assert any(
                w.code == ValidationCodes.SOURCE_FILE_NOT_FOUND for w in result.warnings
            )


# Helper function to create valid Team-Atlanta source structure
def _create_valid_source_structure(source_dir: Path) -> None:
    """Create a valid Team-Atlanta source structure for testing."""
    source_dir.mkdir(parents=True)
    aixcc_dir = source_dir / ".aixcc"
    aixcc_dir.mkdir()

    # Create config.yaml
    config_data = {
        "harness_files": [
            {
                "name": "test_harness",
                "path": "$REPO/test.c",
                "cpvs": [
                    {
                        "name": "cpv_0",
                        "sanitizer": "address",
                        "error_token": "heap-buffer-overflow",
                    }
                ],
            }
        ]
    }
    with open(aixcc_dir / "config.yaml", "w") as f:
        yaml.dump(config_data, f)

    # Create root files
    (source_dir / "build.sh").write_text("#!/bin/bash\necho 'building'")
    (source_dir / "Dockerfile").write_text("FROM ubuntu:20.04")
    (source_dir / "project.yaml").write_text("main_repo: https://github.com/test/repo")

    # Create patches
    patches_dir = aixcc_dir / "patches" / "test_harness"
    patches_dir.mkdir(parents=True)
    (patches_dir / "cpv_0.diff").write_text("--- a/test.c\n+++ b/test.c\n")

    # Create POVs
    povs_dir = aixcc_dir / "povs" / "test_harness"
    povs_dir.mkdir(parents=True)
    (povs_dir / "cpv_0").write_bytes(b"POV binary data")

    # Create crash logs
    logs_dir = aixcc_dir / "crash_logs" / "test_harness"
    logs_dir.mkdir(parents=True)
    (logs_dir / "cpv_0.log").write_text("AddressSanitizer: heap-buffer-overflow")


def _create_valid_target_from_source(source_dir: Path, target_dir: Path) -> None:
    """Create a valid RFC target structure that matches source."""
    target_dir.mkdir(parents=True)
    aixcc_dir = target_dir / ".aixcc"
    aixcc_dir.mkdir()

    # Copy root files
    for filename in ["build.sh", "Dockerfile", "project.yaml"]:
        src = source_dir / filename
        if src.exists():
            (target_dir / filename).write_bytes(src.read_bytes())

    # Create meta.yaml
    meta_config = {
        "harness_files": [
            {
                "name": "test_harness",
                "path": "$REPO/test.c",
                "vulns": [
                    {
                        "vuln_keyword": "cpv_0",
                        "povs": [
                            {
                                "id": "pov_0",
                                "sanitizer": "address",
                                "error_token": "heap-buffer-overflow",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    with open(aixcc_dir / "meta.yaml", "w") as f:
        yaml.dump(meta_config, f)

    # Create CPV directory structure
    cpv_dir = aixcc_dir / "test_harness" / "cpv_0"
    patches_dir = cpv_dir / "patches"
    blobs_dir = cpv_dir / "blobs"
    logs_dir = cpv_dir / "logs"

    patches_dir.mkdir(parents=True)
    blobs_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    # Copy artifacts
    source_aixcc = source_dir / ".aixcc"
    (patches_dir / "patch_0.diff").write_bytes(
        (source_aixcc / "patches" / "test_harness" / "cpv_0.diff").read_bytes()
    )
    (blobs_dir / "pov_0.blob").write_bytes(
        (source_aixcc / "povs" / "test_harness" / "cpv_0").read_bytes()
    )
    (logs_dir / "pov_0.log").write_bytes(
        (source_aixcc / "crash_logs" / "test_harness" / "cpv_0.log").read_bytes()
    )

    # Create vuln.yaml
    vuln_data = {
        "id": "cpv_0",
        "name": "Test Vulnerability",
        "cwes": [],
        "description": "Test",
        "locations": [],
    }
    with open(cpv_dir / "vuln.yaml", "w") as f:
        yaml.dump(vuln_data, f)

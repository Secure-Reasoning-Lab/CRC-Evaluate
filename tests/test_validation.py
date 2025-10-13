"""Tests for the validation module."""

import pytest
from pathlib import Path
from pydantic import ValidationError as PydanticValidationError

from crsbench.validation import validate_benchmark, validate_benchmark_from_string
from crsbench.validation.schemas import POV, Vulnerability, HarnessFile, BenchmarkConfig
from crsbench.validation.errors import ValidationCodes


# ============================================================================
# Schema Validation Tests
# ============================================================================

class TestPOVModel:
    """Test POV model validation."""

    def test_pov_valid_with_error_token(self):
        """Test valid POV with error token."""
        pov = POV(
            id="pov_0",
            sanitizer="address",
            error_token="ERROR: AddressSanitizer: heap-buffer-overflow"
        )
        assert pov.id == "pov_0"
        assert pov.sanitizer == "address"
        assert pov.error_token == "ERROR: AddressSanitizer: heap-buffer-overflow"

    def test_pov_valid_without_error_token(self):
        """Test valid POV without error token (optional)."""
        pov = POV(id="pov_0", sanitizer="address")
        assert pov.id == "pov_0"
        assert pov.sanitizer == "address"
        assert pov.error_token is None

    def test_pov_invalid_sanitizer(self):
        """Test POV with invalid sanitizer type."""
        with pytest.raises(PydanticValidationError) as exc_info:
            POV(id="pov_0", sanitizer="invalid_type")
        assert "sanitizer" in str(exc_info.value).lower()

    def test_pov_empty_id(self):
        """Test POV with empty id."""
        with pytest.raises(PydanticValidationError):
            POV(id="", sanitizer="address")

    def test_pov_empty_error_token(self):
        """Test POV with empty string error token (should fail)."""
        with pytest.raises(PydanticValidationError):
            POV(id="pov_0", sanitizer="address", error_token="")


class TestVulnerabilityModel:
    """Test Vulnerability model validation."""

    def test_vulnerability_valid(self):
        """Test valid vulnerability."""
        vuln = Vulnerability(
            vuln_keyword="buffer_overflow",
            difficulty_level=3,
            povs=[
                POV(id="pov_0", sanitizer="address"),
                POV(id="pov_1", sanitizer="undefined")
            ]
        )
        assert vuln.vuln_keyword == "buffer_overflow"
        assert vuln.difficulty_level == 3
        assert len(vuln.povs) == 2

    def test_vulnerability_without_difficulty(self):
        """Test vulnerability without difficulty level (optional)."""
        vuln = Vulnerability(
            vuln_keyword="use_after_free",
            povs=[POV(id="pov_0", sanitizer="memory")]
        )
        assert vuln.difficulty_level is None

    def test_vulnerability_duplicate_pov_ids(self):
        """Test vulnerability with duplicate POV IDs."""
        with pytest.raises(PydanticValidationError) as exc_info:
            Vulnerability(
                vuln_keyword="buffer_overflow",
                povs=[
                    POV(id="pov_0", sanitizer="address"),
                    POV(id="pov_0", sanitizer="undefined")  # Duplicate
                ]
            )
        assert "duplicate" in str(exc_info.value).lower()

    def test_vulnerability_empty_povs(self):
        """Test vulnerability with no POVs."""
        with pytest.raises(PydanticValidationError):
            Vulnerability(vuln_keyword="buffer_overflow", povs=[])


class TestHarnessFileModel:
    """Test HarnessFile model validation."""

    def test_harness_absolute_path(self):
        """Test harness with absolute path."""
        harness = HarnessFile(
            name="test_harness",
            path="/src/project/test/harness.c"
        )
        assert harness.path == "/src/project/test/harness.c"

    def test_harness_relative_path(self):
        """Test harness with relative path."""
        harness = HarnessFile(
            name="test_harness",
            path="./test/harness.c"
        )
        assert harness.path == "./test/harness.c"

    def test_harness_repo_variable_invalid(self):
        """Test harness with $REPO variable (should fail)."""
        with pytest.raises(PydanticValidationError) as exc_info:
            HarnessFile(
                name="test_harness",
                path="$REPO/test/harness.c"
            )
        assert "absolute" in str(exc_info.value).lower() or "relative" in str(exc_info.value).lower()

    def test_harness_project_variable_invalid(self):
        """Test harness with $PROJECT variable (should fail)."""
        with pytest.raises(PydanticValidationError):
            HarnessFile(
                name="test_harness",
                path="$PROJECT/test/harness.c"
            )

    def test_harness_without_vulns(self):
        """Test harness without vulnerabilities (distractor harness)."""
        harness = HarnessFile(
            name="distractor",
            path="/src/project/test/distractor.c"
        )
        assert harness.vulns == []


# ============================================================================
# Format Validator Tests
# ============================================================================

class TestValidateCorrectFormat:
    """Test validation with correct meta.yaml configurations."""

    def test_validate_minimal_valid(self):
        """Test validation with minimal valid configuration."""
        valid_yaml = """
full_mode:
  base_commit: "abc123def456"

harness_files:
  - name: "test_harness"
    path: "/src/project/test/harness.c"
    vulns:
      - vuln_keyword: "buffer_overflow"
        povs:
          - id: "pov_0"
            sanitizer: "address"
"""
        result = validate_benchmark_from_string(valid_yaml)

        assert result.is_valid is True
        assert result.error_count == 0
        assert result.metadata["total_harnesses"] == 1
        assert result.metadata["total_vulns"] == 1
        assert result.metadata["total_povs"] == 1

    def test_validate_complete_configuration(self):
        """Test validation with complete configuration."""
        valid_yaml = """
patch_exclude_list:
  - "build.sh"
  - "test/**"
  - "**/*test*.c"

delta_mode:
  base_commit: "abc123def456"
  ref_commit: "def456abc123"

full_mode:
  base_commit: "def456abc123"

harness_files:
  - name: "fuzz_parser"
    path: "/src/project/test/fuzz_parser.c"
    vulns:
      - vuln_keyword: "buffer_overflow"
        difficulty_level: 3
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"
          - id: "pov_1"
            sanitizer: "undefined"
            error_token: "runtime error: index out of bounds"
      - vuln_keyword: "use_after_free"
        povs:
          - id: "pov_0"
            sanitizer: "memory"
  - name: "fuzz_network"
    path: "/src/project/test/fuzz_network.c"
"""
        result = validate_benchmark_from_string(valid_yaml)

        assert result.is_valid is True
        assert result.error_count == 0
        assert result.metadata["total_harnesses"] == 2
        assert result.metadata["total_vulns"] == 2
        assert result.metadata["total_povs"] == 3
        assert result.metadata["has_delta_mode"] is True
        assert result.metadata["has_full_mode"] is True
        assert result.metadata["patch_exclude_patterns"] == 3

    def test_validate_optional_error_token(self):
        """Test validation with optional error_token."""
        valid_yaml = """
full_mode:
  base_commit: "abc123def"

harness_files:
  - name: "test"
    path: "/src/test.c"
    vulns:
      - vuln_keyword: "buffer_overflow"
        povs:
          - id: "pov_0"
            sanitizer: "address"
          - id: "pov_1"
            sanitizer: "undefined"
            error_token: "runtime error"
"""
        result = validate_benchmark_from_string(valid_yaml)

        assert result.is_valid is True
        assert result.metadata["total_povs"] == 2


class TestValidateIncorrectFormat:
    """Test validation with incorrect configurations."""

    def test_validate_missing_harness_files(self):
        """Test validation with missing harness_files."""
        invalid_yaml = """
patch_exclude_list:
  - "build.sh"

full_mode:
  base_commit: "abc123def456"
"""
        result = validate_benchmark_from_string(invalid_yaml)

        assert result.is_valid is False
        assert result.error_count > 0
        error_messages = [error.message for error in result.errors]
        assert any("harness_files" in msg.lower() for msg in error_messages)

    def test_validate_missing_evaluation_mode(self):
        """Test validation with no evaluation mode."""
        invalid_yaml = """
harness_files:
  - name: "test"
    path: "/src/test.c"
"""
        result = validate_benchmark_from_string(invalid_yaml)

        assert result.is_valid is False
        # Error is caught at schema level (BenchmarkConfig.__init__)
        assert any("evaluation mode" in e.message.lower() or "delta_mode" in e.message.lower() or "full_mode" in e.message.lower() for e in result.errors)

    def test_validate_invalid_commit_hash(self):
        """Test validation with invalid commit hash."""
        invalid_yaml = """
full_mode:
  base_commit: "invalid_hash!"

harness_files:
  - name: "test"
    path: "/src/test.c"
"""
        result = validate_benchmark_from_string(invalid_yaml)

        assert result.is_valid is False
        assert any("commit" in e.message.lower() for e in result.errors)

    def test_validate_duplicate_harness_names(self):
        """Test validation with duplicate harness names."""
        invalid_yaml = """
full_mode:
  base_commit: "abc123def"

harness_files:
  - name: "test"
    path: "/src/test1.c"
  - name: "test"
    path: "/src/test2.c"
"""
        result = validate_benchmark_from_string(invalid_yaml)

        assert result.is_valid is False
        assert any("duplicate" in e.message.lower() for e in result.errors)

    def test_validate_same_base_ref_commits(self):
        """Test validation with same base and ref commits in delta mode."""
        invalid_yaml = """
delta_mode:
  base_commit: "abc123def456"
  ref_commit: "abc123def456"

harness_files:
  - name: "test"
    path: "/src/test.c"
"""
        result = validate_benchmark_from_string(invalid_yaml)

        assert result.is_valid is False
        assert any("same" in e.message.lower() for e in result.errors)


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestErrorHandling:
    """Test error handling and graceful degradation."""

    def test_invalid_yaml_syntax(self):
        """Test handling of invalid YAML syntax."""
        invalid_yaml = "invalid: yaml: syntax:"
        result = validate_benchmark_from_string(invalid_yaml)

        assert result.is_valid is False
        assert result.error_count > 0
        assert any(e.code == ValidationCodes.YAML_SYNTAX_ERROR for e in result.errors)

    def test_empty_yaml(self):
        """Test handling of empty YAML."""
        result = validate_benchmark_from_string("")

        assert result.is_valid is False
        assert any(e.code == ValidationCodes.EMPTY_FILE for e in result.errors)

    def test_graceful_degradation(self):
        """Test that validation continues after finding errors."""
        invalid_yaml = """
full_mode:
  base_commit: "invalid!"

harness_files:
  - name: ""
    path: "invalid_path"
    vulns:
      - vuln_keyword: ""
        povs: []
"""
        result = validate_benchmark_from_string(invalid_yaml)

        # Should find multiple errors, not just the first one
        assert result.is_valid is False
        assert result.error_count > 1


# ============================================================================
# Path Validation Tests
# ============================================================================

class TestPathValidation:
    """Test path validation rules."""

    def test_absolute_path_valid(self):
        """Test absolute paths are valid."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "/src/project/test/harness.c"
"""
        result = validate_benchmark_from_string(yaml_content)
        assert result.is_valid is True

    def test_relative_path_valid(self):
        """Test relative paths starting with ./ are valid."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "./test/harness.c"
"""
        result = validate_benchmark_from_string(yaml_content)
        assert result.is_valid is True

    def test_repo_variable_rejected(self):
        """Test $REPO/ paths are rejected."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "$REPO/test/harness.c"
"""
        result = validate_benchmark_from_string(yaml_content)
        assert result.is_valid is False

    def test_project_variable_rejected(self):
        """Test $PROJECT/ paths are rejected."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "$PROJECT/test/harness.c"
"""
        result = validate_benchmark_from_string(yaml_content)
        assert result.is_valid is False


# ============================================================================
# Metadata Generation Tests
# ============================================================================

class TestMetadataGeneration:
    """Test metadata generation."""

    def test_metadata_counts(self):
        """Test metadata counts are correct."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "h1"
    path: "/src/h1.c"
    vulns:
      - vuln_keyword: "v1"
        povs:
          - id: "pov_0"
            sanitizer: "address"
          - id: "pov_1"
            sanitizer: "undefined"
      - vuln_keyword: "v2"
        povs:
          - id: "pov_0"
            sanitizer: "memory"
  - name: "h2"
    path: "/src/h2.c"
    vulns:
      - vuln_keyword: "v3"
        povs:
          - id: "pov_0"
            sanitizer: "address"
"""
        result = validate_benchmark_from_string(yaml_content)

        assert result.is_valid is True
        assert result.metadata["total_harnesses"] == 2
        assert result.metadata["total_vulns"] == 3
        assert result.metadata["total_povs"] == 4

    def test_metadata_with_distractor_harnesses(self):
        """Test metadata with harnesses without vulnerabilities."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "real"
    path: "/src/real.c"
    vulns:
      - vuln_keyword: "buffer_overflow"
        povs:
          - id: "pov_0"
            sanitizer: "address"
  - name: "distractor"
    path: "/src/distractor.c"
"""
        result = validate_benchmark_from_string(yaml_content)

        assert result.is_valid is True
        assert result.metadata["total_harnesses"] == 2
        assert result.metadata["total_vulns"] == 1
        assert result.metadata["total_povs"] == 1


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests with actual files."""

    def test_validate_meta_example_yaml(self):
        """Test validation of docs/meta-example.yaml."""
        # Can use either direct path or symlink in tests/assets
        result = validate_benchmark("tests/assets/meta-example.yaml")

        assert result.is_valid is True
        assert result.metadata["total_harnesses"] == 2
        assert result.metadata["total_vulns"] == 2
        assert result.metadata["total_povs"] == 3
        assert result.metadata["has_delta_mode"] is True
        assert result.metadata["has_full_mode"] is True

    def test_validation_result_serialization(self):
        """Test that validation results can be serialized."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "/src/test.c"
"""
        result = validate_benchmark_from_string(yaml_content)

        # Should be able to serialize to dict
        result_dict = result.to_dict()
        assert isinstance(result_dict, dict)
        assert "is_valid" in result_dict
        assert "issues" in result_dict
        assert "metadata" in result_dict

    def test_validation_summary(self):
        """Test validation summary generation."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: ""
    path: "/src/test.c"
"""
        result = validate_benchmark_from_string(yaml_content)

        summary = result.summary()
        assert isinstance(summary, str)
        assert "INVALID" in summary or "error" in summary.lower()
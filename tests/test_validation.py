"""Tests for the validation module."""

import pytest
from pathlib import Path
from pydantic import ValidationError as PydanticValidationError

from crsbench.validation import (
    validate_benchmark,
    validate_benchmark_from_string,
    validate_experiment_config,
    validate_experiment_config_from_string,
    validate_benchmark_suite,
    validate_benchmark_suite_from_string
)
from crsbench.validation.schemas import POV, Vulnerability, HarnessFile, BenchmarkConfig, ExperimentConfig, BenchmarkSuiteConfig
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
        # Use Path to construct path relative to test file
        test_dir = Path(__file__).parent
        meta_path = test_dir / "assets" / "meta-example.yaml"
        result = validate_benchmark(meta_path)

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


# ============================================================================
# Experiment Config Tests
# ============================================================================

class TestExperimentConfigSchema:
    """Test ExperimentConfig schema validation."""

    def test_experiment_config_valid(self):
        """Test valid experiment config."""
        config = ExperimentConfig(
            trials=3,
            max_total_time=86400,
            difficulty_level=2,
            experiment_filestore="/tmp/experiment-data",
            report_filestore="/tmp/report-data"
        )
        assert config.trials == 3
        assert config.max_total_time == 86400
        assert config.difficulty_level == 2

    def test_experiment_config_invalid_trials(self):
        """Test experiment config with invalid trials."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                trials=0,  # Invalid: must be >= 1
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep"
            )
        assert "trials" in str(exc_info.value).lower()

    def test_experiment_config_invalid_time(self):
        """Test experiment config with invalid max_total_time."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                trials=1,
                max_total_time=0,  # Invalid: must be >= 1
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep"
            )
        assert "max_total_time" in str(exc_info.value).lower()

    def test_experiment_config_invalid_difficulty(self):
        """Test experiment config with invalid difficulty level."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                trials=1,
                max_total_time=86400,
                difficulty_level=5,  # Invalid: must be 0-4
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep"
            )
        assert "difficulty_level" in str(exc_info.value).lower()

    def test_experiment_config_empty_filestore(self):
        """Test experiment config with empty filestore paths."""
        with pytest.raises(PydanticValidationError):
            ExperimentConfig(
                trials=1,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="",  # Empty
                report_filestore="/tmp/rep"
            )

    def test_experiment_config_with_redis_host(self):
        """Test experiment config with redis_host field."""
        config = ExperimentConfig(
            trials=1,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            redis_host="localhost"
        )
        assert config.redis_host == "localhost"

    def test_experiment_config_without_redis_host(self):
        """Test experiment config without redis_host (defaults to None)."""
        config = ExperimentConfig(
            trials=1,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep"
        )
        assert config.redis_host is None

    def test_experiment_config_redis_host_none_string(self):
        """Test experiment config with redis_host='none' (converted to None)."""
        config = ExperimentConfig(
            trials=1,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            redis_host="none"
        )
        assert config.redis_host is None

    def test_experiment_config_with_benchmarks_root(self):
        """Test experiment config with valid benchmarks_root."""
        import tempfile
        import os

        # Create a temporary directory for testing
        with tempfile.TemporaryDirectory() as tmpdir:
            config = ExperimentConfig(
                trials=1,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                benchmarks_root=tmpdir
            )
            # Should return absolute path
            assert config.benchmarks_root == str(Path(tmpdir).absolute())

    def test_experiment_config_invalid_benchmarks_root(self):
        """Test experiment config with non-existent benchmarks_root."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                trials=1,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                benchmarks_root="/nonexistent/path"
            )
        assert "does not exist" in str(exc_info.value).lower()

    def test_experiment_config_benchmarks_root_not_directory(self):
        """Test experiment config with benchmarks_root pointing to a file."""
        import tempfile

        # Create a temporary file (not directory)
        with tempfile.NamedTemporaryFile(delete=False) as tmpfile:
            tmpfile_path = tmpfile.name

        try:
            with pytest.raises(PydanticValidationError) as exc_info:
                ExperimentConfig(
                    trials=1,
                    max_total_time=86400,
                    difficulty_level=1,
                    experiment_filestore="/tmp/exp",
                    report_filestore="/tmp/rep",
                    benchmarks_root=tmpfile_path
                )
            assert "must be a directory" in str(exc_info.value).lower()
        finally:
            Path(tmpfile_path).unlink()

    def test_experiment_config_to_dict(self):
        """Test experiment config to_dict() method."""
        config = ExperimentConfig(
            trials=3,
            max_total_time=86400,
            difficulty_level=2,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            redis_host="redis-server"
        )
        config_dict = config.to_dict()

        assert isinstance(config_dict, dict)
        assert config_dict['trials'] == 3
        assert config_dict['max_total_time'] == 86400
        assert config_dict['difficulty_level'] == 2
        assert config_dict['redis_host'] == "redis-server"
        assert config_dict['benchmarks_root'] is None


class TestExperimentConfigValidation:
    """Test experiment config validation with YAML."""

    def test_validate_experiment_valid(self):
        """Test validation with valid experiment config."""
        yaml_content = """
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is True
        assert result.error_count == 0
        assert result.metadata["trials"] == 1
        assert result.metadata["max_total_time"] == 86400
        assert result.metadata["difficulty_level"] == 1

    def test_validate_experiment_missing_field(self):
        """Test validation with missing required field."""
        yaml_content = """
trials: 1
max_total_time: 86400
# Missing difficulty_level
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is False
        assert result.error_count > 0
        assert any("difficulty_level" in e.message.lower() for e in result.errors)

    def test_validate_experiment_negative_trials(self):
        """Test validation with negative trials."""
        yaml_content = """
trials: -1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is False
        assert any("trials" in e.message.lower() for e in result.errors)

    def test_validate_experiment_out_of_range_difficulty(self):
        """Test validation with out of range difficulty level."""
        yaml_content = """
trials: 1
max_total_time: 86400
difficulty_level: 10
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is False
        assert any("difficulty_level" in e.message.lower() for e in result.errors)

    def test_validate_experiment_empty_yaml(self):
        """Test validation with empty YAML."""
        result = validate_experiment_config_from_string("")

        assert result.is_valid is False
        assert any(e.code == ValidationCodes.EMPTY_FILE for e in result.errors)

    def test_validate_experiment_invalid_yaml_syntax(self):
        """Test validation with invalid YAML syntax."""
        invalid_yaml = "trials: [invalid: yaml"
        result = validate_experiment_config_from_string(invalid_yaml)

        assert result.is_valid is False
        assert any(e.code == ValidationCodes.YAML_SYNTAX_ERROR for e in result.errors)

    def test_validate_experiment_with_redis_host(self):
        """Test validation with redis_host field."""
        yaml_content = """
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
redis_host: queue-server
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is True
        assert result.metadata.get("redis_host") == "queue-server"

    def test_validate_experiment_with_benchmarks_root(self):
        """Test validation with benchmarks_root field."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_content = f"""
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
benchmarks_root: {tmpdir}
"""
            result = validate_experiment_config_from_string(yaml_content)

            assert result.is_valid is True
            assert result.metadata.get("benchmarks_root") == str(Path(tmpdir).absolute())

    def test_validate_experiment_redis_none_for_local_mode(self):
        """Test validation with redis_host set to 'none' for local mode."""
        yaml_content = """
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
redis_host: none
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is True
        # 'none' should be converted to None
        assert result.metadata.get("redis_host") is None


# ============================================================================
# Benchmark Suite Config Tests
# ============================================================================

class TestBenchmarkSuiteSchema:
    """Test BenchmarkSuiteConfig schema validation."""

    def test_benchmark_suite_valid(self):
        """Test valid benchmark suite config."""
        config = BenchmarkSuiteConfig(
            Name="crsbench-c",
            Description="A benchmark suite for evaluating C/C++ CRS",
            benchmark_list=["bench1", "bench2", "bench3"],
            release_date="09.23.2025"
        )
        assert config.Name == "crsbench-c"
        assert config.Description == "A benchmark suite for evaluating C/C++ CRS"
        assert len(config.benchmark_list) == 3
        assert config.release_date == "09.23.2025"

    def test_benchmark_suite_with_alias(self):
        """Test benchmark suite with 'Release date' field alias."""
        config = BenchmarkSuiteConfig(**{
            "Name": "test-suite",
            "Description": "Test suite",
            "benchmark_list": ["bench1"],
            "Release date": "01.01.2025"  # Using aliased field name
        })
        assert config.release_date == "01.01.2025"

    def test_benchmark_suite_empty_name(self):
        """Test benchmark suite with empty name."""
        with pytest.raises(PydanticValidationError):
            BenchmarkSuiteConfig(
                Name="",
                Description="Test",
                benchmark_list=["bench1"],
                release_date="01.01.2025"
            )

    def test_benchmark_suite_empty_benchmark_list(self):
        """Test benchmark suite with empty benchmark list."""
        with pytest.raises(PydanticValidationError):
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test",
                benchmark_list=[],  # Empty
                release_date="01.01.2025"
            )

    def test_benchmark_suite_invalid_date_format(self):
        """Test benchmark suite with invalid date format."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test",
                benchmark_list=["bench1"],
                release_date="2025-09-23"  # Wrong format
            )
        assert "release date" in str(exc_info.value).lower() or "format" in str(exc_info.value).lower()

    def test_benchmark_suite_duplicate_benchmarks(self):
        """Test benchmark suite with duplicate benchmark IDs."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test",
                benchmark_list=["bench1", "bench1"],  # Duplicate
                release_date="01.01.2025"
            )
        assert "duplicate" in str(exc_info.value).lower()


class TestBenchmarkSuiteValidation:
    """Test benchmark suite validation with YAML."""

    def test_validate_suite_valid(self):
        """Test validation with valid benchmark suite config."""
        yaml_content = """
Name: crsbench-c
Description: A benchmark suite for evaluating C/C++ CRS
Release date: 09.23.2025
benchmark_list:
  - benchmark_id_1
  - benchmark_id_2
  - benchmark_id_3
"""
        result = validate_benchmark_suite_from_string(yaml_content)

        assert result.is_valid is True
        assert result.error_count == 0
        assert result.metadata["suite_name"] == "crsbench-c"
        assert result.metadata["total_benchmarks"] == 3
        assert result.metadata["release_date"] == "09.23.2025"

    def test_validate_suite_missing_name(self):
        """Test validation with missing Name field."""
        yaml_content = """
Description: Test suite
Release date: 01.01.2025
benchmark_list:
  - bench1
"""
        result = validate_benchmark_suite_from_string(yaml_content)

        assert result.is_valid is False
        assert any("name" in e.message.lower() for e in result.errors)

    def test_validate_suite_invalid_date(self):
        """Test validation with invalid date format."""
        yaml_content = """
Name: test-suite
Description: Test suite
Release date: 2025/09/23
benchmark_list:
  - bench1
"""
        result = validate_benchmark_suite_from_string(yaml_content)

        assert result.is_valid is False
        assert any("release date" in e.message.lower() or "format" in e.message.lower() for e in result.errors)

    def test_validate_suite_empty_benchmark_list(self):
        """Test validation with empty benchmark list."""
        yaml_content = """
Name: test-suite
Description: Test suite
Release date: 01.01.2025
benchmark_list: []
"""
        result = validate_benchmark_suite_from_string(yaml_content)

        assert result.is_valid is False
        assert any("benchmark_list" in e.message.lower() for e in result.errors)

    def test_validate_suite_duplicate_benchmarks(self):
        """Test validation with duplicate benchmark IDs."""
        yaml_content = """
Name: test-suite
Description: Test suite
Release date: 01.01.2025
benchmark_list:
  - bench1
  - bench2
  - bench1
"""
        result = validate_benchmark_suite_from_string(yaml_content)

        assert result.is_valid is False
        assert any("duplicate" in e.message.lower() for e in result.errors)

    def test_validate_suite_empty_yaml(self):
        """Test validation with empty YAML."""
        result = validate_benchmark_suite_from_string("")

        assert result.is_valid is False
        assert any(e.code == ValidationCodes.EMPTY_FILE for e in result.errors)

    def test_validate_suite_invalid_yaml_syntax(self):
        """Test validation with invalid YAML syntax."""
        invalid_yaml = "Name: [invalid: yaml"
        result = validate_benchmark_suite_from_string(invalid_yaml)

        assert result.is_valid is False
        assert any(e.code == ValidationCodes.YAML_SYNTAX_ERROR for e in result.errors)


# ============================================================================
# Integration Tests for All Config Types
# ============================================================================

class TestIntegrationAllConfigs:
    """Integration tests with all configuration types."""

    def test_validate_experiment_example_yaml(self):
        """Test validation of docs/experiment-example.yaml."""
        # Use Path to construct path relative to test file
        test_dir = Path(__file__).parent
        exp_path = test_dir / "assets" / "experiment-example.yaml"
        result = validate_experiment_config(exp_path)

        assert result.is_valid is True
        assert result.metadata["trials"] == 1
        assert result.metadata["max_total_time"] == 86400
        assert result.metadata["difficulty_level"] == 1

    def test_validate_suite_example_format(self):
        """Test validation of benchmark suite with proper format (not placeholders)."""
        # The example file contains YAML placeholders {benchmark_id_X} which are invalid
        # So test with a proper YAML string instead
        suite_yaml = """
Name: crsbench-c
Description: A benchmark suite for evaluating C/C++ CRS.
Release date: 09.23.2025
benchmark_list:
  - benchmark_id_1
  - benchmark_id_2
  - benchmark_id_3
"""
        result = validate_benchmark_suite_from_string(suite_yaml)

        assert result.is_valid is True
        assert result.metadata["suite_name"] == "crsbench-c"
        assert "C/C++" in result.metadata["suite_description"]
        assert result.metadata["release_date"] == "09.23.2025"

    def test_all_config_types_serialization(self):
        """Test that all config validation results can be serialized."""
        # Benchmark config
        benchmark_yaml = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "/src/test.c"
"""
        benchmark_result = validate_benchmark_from_string(benchmark_yaml)
        assert isinstance(benchmark_result.to_dict(), dict)

        # Experiment config
        experiment_yaml = """
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
"""
        experiment_result = validate_experiment_config_from_string(experiment_yaml)
        assert isinstance(experiment_result.to_dict(), dict)

        # Benchmark suite config
        suite_yaml = """
Name: test-suite
Description: Test suite
Release date: 01.01.2025
benchmark_list:
  - bench1
"""
        suite_result = validate_benchmark_suite_from_string(suite_yaml)
        assert isinstance(suite_result.to_dict(), dict)
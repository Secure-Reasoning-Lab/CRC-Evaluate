"""Tests for the validation module."""

from pathlib import Path

import pytest
from crsbench.validation import (
    validate_benchmark,
    validate_benchmark_from_string,
    validate_benchmark_suite_from_string,
    validate_experiment_config,
    validate_experiment_config_from_string,
)
from crsbench.validation.errors import ValidationCodes
from crsbench.validation.schemas import (
    POV,
    BenchmarkEntry,
    BenchmarkSuiteConfig,
    EvaluationMode,
    ExperimentConfig,
    HarnessFile,
    ProjectConfig,
    RtsMode,
    Vulnerability,
)
from pydantic import ValidationError as PydanticValidationError

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
            error_token="ERROR: AddressSanitizer: heap-buffer-overflow",
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
                POV(id="pov_1", sanitizer="undefined"),
            ],
        )
        assert vuln.vuln_keyword == "buffer_overflow"
        assert vuln.difficulty_level == 3
        assert len(vuln.povs) == 2

    def test_vulnerability_without_difficulty(self):
        """Test vulnerability without difficulty level (optional)."""
        vuln = Vulnerability(
            vuln_keyword="use_after_free", povs=[POV(id="pov_0", sanitizer="memory")]
        )
        assert vuln.difficulty_level is None

    def test_vulnerability_duplicate_pov_ids(self):
        """Test vulnerability with duplicate POV IDs."""
        with pytest.raises(PydanticValidationError) as exc_info:
            Vulnerability(
                vuln_keyword="buffer_overflow",
                povs=[
                    POV(id="pov_0", sanitizer="address"),
                    POV(id="pov_0", sanitizer="undefined"),  # Duplicate
                ],
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
        harness = HarnessFile(name="test_harness", path="/src/project/test/harness.c")
        assert harness.path == "/src/project/test/harness.c"

    def test_harness_relative_path(self):
        """Test harness with relative path."""
        harness = HarnessFile(name="test_harness", path="./test/harness.c")
        assert harness.path == "./test/harness.c"

    def test_harness_repo_variable_valid(self):
        """Test harness with $REPO variable (should pass)."""
        harness = HarnessFile(name="test_harness", path="$REPO/test/harness.c")
        assert harness.path == "$REPO/test/harness.c"

    def test_harness_project_variable_valid(self):
        """Test harness with $PROJECT variable (should pass)."""
        harness = HarnessFile(name="test_harness", path="$PROJECT/test/harness.c")
        assert harness.path == "$PROJECT/test/harness.c"

    def test_harness_without_vulns(self):
        """Test harness without vulnerabilities (distractor harness)."""
        harness = HarnessFile(name="distractor", path="/src/project/test/distractor.c")
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
        assert any(
            "evaluation mode" in e.message.lower()
            or "delta_mode" in e.message.lower()
            or "full_mode" in e.message.lower()
            for e in result.errors
        )

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

    def test_repo_variable_accepted(self):
        """Test $REPO/ paths are accepted."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "$REPO/test/harness.c"
"""
        result = validate_benchmark_from_string(yaml_content)
        assert result.is_valid is True

    def test_project_variable_accepted(self):
        """Test $PROJECT/ paths are accepted."""
        yaml_content = """
full_mode:
  base_commit: "abc123def"
harness_files:
  - name: "test"
    path: "$PROJECT/test/harness.c"
"""
        result = validate_benchmark_from_string(yaml_content)
        assert result.is_valid is True


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
            experiment="test",
            trials=3,
            mode=EvaluationMode.DELTA,
            max_total_time=86400,
            difficulty_level=2,
            experiment_filestore="/tmp/experiment-data",
            report_filestore="/tmp/report-data",
            crses=["test-crs"],
            benchmarks=["test-bench"],
        )
        assert config.trials == 3
        assert config.max_total_time == 86400
        assert config.difficulty_level == 2

    def test_experiment_config_invalid_trials(self):
        """Test experiment config with invalid trials."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                experiment="test",
                trials=0,  # Invalid: must be >= 1
                mode=EvaluationMode.DELTA,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
            )
        assert "trials" in str(exc_info.value).lower()

    def test_experiment_config_invalid_time(self):
        """Test experiment config with invalid max_total_time."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode=EvaluationMode.DELTA,
                max_total_time=0,  # Invalid: must be >= 1
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
            )
        assert "max_total_time" in str(exc_info.value).lower()

    def test_experiment_config_invalid_difficulty(self):
        """Test experiment config with invalid difficulty level."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode=EvaluationMode.DELTA,
                max_total_time=86400,
                difficulty_level=5,  # Invalid: must be 0-4
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
            )
        assert "difficulty_level" in str(exc_info.value).lower()

    def test_experiment_config_empty_filestore(self):
        """Test experiment config with empty filestore paths."""
        with pytest.raises(PydanticValidationError):
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode=EvaluationMode.DELTA,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="",  # Empty
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
            )

    def test_experiment_config_with_redis_host(self):
        """Test experiment config with redis_host field."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
            redis_host="localhost",
        )
        assert config.redis_host == "localhost"

    def test_experiment_config_without_redis_host(self):
        """Test experiment config without redis_host (defaults to None)."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
        )
        assert config.redis_host is None

    def test_experiment_config_redis_host_none_string(self):
        """Test experiment config with redis_host='none' (converted to None)."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
            redis_host="none",
        )
        assert config.redis_host is None

    def test_experiment_config_with_benchmarks_root(self):
        """Test experiment config with valid benchmarks_root."""
        import tempfile

        # Create a temporary directory for testing
        with tempfile.TemporaryDirectory() as tmpdir:
            config = ExperimentConfig(
                experiment="test",
                trials=1,
                mode=EvaluationMode.DELTA,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
                benchmarks_root=tmpdir,
            )
            # Should return absolute path as Path object
            assert config.benchmarks_root == Path(tmpdir).absolute()

    def test_experiment_config_invalid_benchmarks_root(self):
        """Test experiment config with non-existent benchmarks_root."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode=EvaluationMode.DELTA,
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
                benchmarks_root="/nonexistent/path",
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
                    experiment="test",
                    trials=1,
                    mode=EvaluationMode.DELTA,
                    max_total_time=86400,
                    difficulty_level=1,
                    experiment_filestore="/tmp/exp",
                    report_filestore="/tmp/rep",
                    crses=["test-crs"],
                    benchmarks=["test-bench"],
                    benchmarks_root=tmpfile_path,
                )
            assert "must be a directory" in str(exc_info.value).lower()
        finally:
            Path(tmpfile_path).unlink()

    def test_experiment_config_to_dict(self):
        """Test experiment config to_dict() method."""
        config = ExperimentConfig(
            experiment="test",
            trials=3,
            mode=EvaluationMode.DELTA,
            max_total_time=86400,
            difficulty_level=2,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
            redis_host="redis-server",
        )
        config_dict = config.to_dict()

        assert isinstance(config_dict, dict)
        assert config_dict["trials"] == 3
        assert config_dict["mode"] == "delta"
        assert config_dict["max_total_time"] == 86400
        assert config_dict["difficulty_level"] == 2
        assert config_dict["redis_host"] == "redis-server"
        assert config_dict["benchmarks_root"] is None

    def test_experiment_config_only_cpv_harnesses_default(self):
        """Test that only_cpv_harnesses defaults to True."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
        )
        assert config.only_cpv_harnesses is True

    def test_experiment_config_only_cpv_harnesses_explicit_false(self):
        """Test explicit only_cpv_harnesses=False."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
            only_cpv_harnesses=False,
        )
        assert config.only_cpv_harnesses is False

    def test_experiment_config_only_cpv_harnesses_explicit_true(self):
        """Test explicit only_cpv_harnesses=True."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
            only_cpv_harnesses=True,
        )
        assert config.only_cpv_harnesses is True

    def test_experiment_config_only_cpv_harnesses_in_to_dict(self):
        """Test that only_cpv_harnesses is included in to_dict()."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
            only_cpv_harnesses=False,
        )
        config_dict = config.to_dict()
        assert "only_cpv_harnesses" in config_dict
        assert config_dict["only_cpv_harnesses"] is False

    def test_max_total_time_validation_success(self):
        """Test that max_total_time > sum of timeouts passes validation."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode=EvaluationMode.DELTA,
            max_total_time=20000,  # Greater than 3600 + 7200 + 7200 = 18000
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["test-crs"],
            benchmarks=["test-bench"],
        )
        # Should not raise validation error
        assert config.max_total_time == 20000

    def test_max_total_time_validation_failure(self):
        """Test that max_total_time <= sum of timeouts fails validation."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode=EvaluationMode.DELTA,
                max_total_time=18000,  # Equal to 3600 + 7200 + 7200
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["test-crs"],
                benchmarks=["test-bench"],
            )
        # Check that the error message mentions the validation
        error_msg = str(exc_info.value)
        assert "max_total_time" in error_msg
        assert "build_timeout + run_timeout + verify_timeout" in error_msg


class TestExperimentConfigValidation:
    """Test experiment config validation with YAML."""

    def test_validate_experiment_valid(self):
        """Test validation with valid experiment config."""
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - test-crs
benchmarks:
  - test-bench
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
experiment: test
trials: 1
mode: delta
max_total_time: 86400
# Missing difficulty_level
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - test-crs
benchmarks:
  - test-bench
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is False
        assert result.error_count > 0
        assert any("difficulty_level" in e.message.lower() for e in result.errors)

    def test_validate_experiment_negative_trials(self):
        """Test validation with negative trials."""
        yaml_content = """
experiment: test
trials: -1
mode: delta
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - test-crs
benchmarks:
  - test-bench
"""
        result = validate_experiment_config_from_string(yaml_content)

        assert result.is_valid is False
        assert any("trials" in e.message.lower() for e in result.errors)

    def test_validate_experiment_out_of_range_difficulty(self):
        """Test validation with out of range difficulty level."""
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
difficulty_level: 10
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - test-crs
benchmarks:
  - test-bench
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
experiment: test
trials: 1
mode: delta
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - test-crs
benchmarks:
  - test-bench
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
experiment: test
trials: 1
mode: delta
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - test-crs
benchmarks:
  - test-bench
benchmarks_root: {tmpdir}
"""
            result = validate_experiment_config_from_string(yaml_content)

            assert result.is_valid is True
            assert result.metadata.get("benchmarks_root") == Path(tmpdir).absolute()

    def test_validate_experiment_redis_none_for_local_mode(self):
        """Test validation with redis_host set to 'none' for local mode."""
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - test-crs
benchmarks:
  - test-bench
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
            release_date="09.23.2025",
        )
        assert config.Name == "crsbench-c"
        assert config.Description == "A benchmark suite for evaluating C/C++ CRS"
        assert len(config.benchmark_list) == 3
        assert config.release_date == "09.23.2025"

    def test_benchmark_suite_with_alias(self):
        """Test benchmark suite with 'Release date' field alias."""
        config = BenchmarkSuiteConfig(
            **{
                "Name": "test-suite",
                "Description": "Test suite",
                "benchmark_list": ["bench1"],
                "Release date": "01.01.2025",  # Using aliased field name
            }
        )
        assert config.release_date == "01.01.2025"

    def test_benchmark_suite_empty_name(self):
        """Test benchmark suite with empty name."""
        with pytest.raises(PydanticValidationError):
            BenchmarkSuiteConfig(
                **{
                    "Name": "",
                    "Description": "Test",
                    "benchmark_list": ["bench1"],
                    "Release date": "01.01.2025",
                }
            )

    def test_benchmark_suite_empty_benchmark_list(self):
        """Test benchmark suite with empty benchmark list."""
        with pytest.raises(PydanticValidationError):
            BenchmarkSuiteConfig(
                **{
                    "Name": "test-suite",
                    "Description": "Test",
                    "benchmark_list": [],  # Empty
                    "Release date": "01.01.2025",
                }
            )

    def test_benchmark_suite_invalid_date_format(self):
        """Test benchmark suite with invalid date format."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                **{
                    "Name": "test-suite",
                    "Description": "Test",
                    "benchmark_list": ["bench1"],
                    "Release date": "2025-09-23",  # Wrong format
                }
            )
        assert (
            "release date" in str(exc_info.value).lower()
            or "format" in str(exc_info.value).lower()
        )

    def test_benchmark_suite_duplicate_benchmarks(self):
        """Test benchmark suite with duplicate benchmark IDs."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                **{
                    "Name": "test-suite",
                    "Description": "Test",
                    "benchmark_list": ["bench1", "bench1"],  # Duplicate
                    "Release date": "01.01.2025",
                }
            )
        assert "duplicate" in str(exc_info.value).lower()


class TestBenchmarkListFormat:
    """Test benchmark_list format with optional harness specification."""

    def test_simple_format(self):
        """Test simple string format (all harnesses)."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=["bench1", "bench2"],
            release_date="01.01.2025",
        )
        assert len(config.benchmark_list) == 2
        assert config.benchmark_list[0] == "bench1"
        assert config.benchmark_list[1] == "bench2"

    def test_extended_format(self):
        """Test extended dict format (specific harnesses)."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[{"bench1": ["harness1", "harness2"]}],
            release_date="01.01.2025",
        )
        assert len(config.benchmark_list) == 1
        assert isinstance(config.benchmark_list[0], dict)
        assert "bench1" in config.benchmark_list[0]
        assert config.benchmark_list[0]["bench1"] == ["harness1", "harness2"]

    def test_mixed_format(self):
        """Test mixed format (both simple and extended)."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[
                "bench1",  # Simple format
                {"bench2": ["harness1", "harness2"]},  # Extended format
                "bench3",  # Simple format
            ],
            release_date="01.01.2025",
        )
        assert len(config.benchmark_list) == 3
        assert config.benchmark_list[0] == "bench1"
        assert isinstance(config.benchmark_list[1], dict)
        assert config.benchmark_list[2] == "bench3"

    def test_get_benchmark_names(self):
        """Test get_benchmark_names() extracts names correctly."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[
                "bench1",
                {"bench2": ["harness1"]},
                "bench3",
            ],
            release_date="01.01.2025",
        )
        names = config.get_benchmark_names()
        assert names == ["bench1", "bench2", "bench3"]

    def test_get_benchmark_entries_simple(self):
        """Test get_benchmark_entries() with simple format."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=["bench1", "bench2"],
            release_date="01.01.2025",
        )
        entries = config.get_benchmark_entries()
        assert len(entries) == 2
        assert all(isinstance(e, BenchmarkEntry) for e in entries)
        assert entries[0].name == "bench1"
        assert entries[0].harnesses is None  # All harnesses
        assert entries[1].name == "bench2"
        assert entries[1].harnesses is None  # All harnesses

    def test_get_benchmark_entries_extended(self):
        """Test get_benchmark_entries() with extended format."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[{"bench1": ["harness1", "harness2"]}],
            release_date="01.01.2025",
        )
        entries = config.get_benchmark_entries()
        assert len(entries) == 1
        assert isinstance(entries[0], BenchmarkEntry)
        assert entries[0].name == "bench1"
        assert entries[0].harnesses == ["harness1", "harness2"]

    def test_get_benchmark_entries_mixed(self):
        """Test get_benchmark_entries() with mixed format."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[
                "bench1",
                {"bench2": ["harness1", "harness2"]},
                "bench3",
            ],
            release_date="01.01.2025",
        )
        entries = config.get_benchmark_entries()
        assert len(entries) == 3
        # First entry: simple format
        assert entries[0].name == "bench1"
        assert entries[0].harnesses is None
        # Second entry: extended format
        assert entries[1].name == "bench2"
        assert entries[1].harnesses == ["harness1", "harness2"]
        # Third entry: simple format
        assert entries[2].name == "bench3"
        assert entries[2].harnesses is None

    def test_empty_harness_list_rejected(self):
        """Test that empty harness list is rejected."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test suite",
                benchmark_list=[{"bench1": []}],  # Empty harness list
                release_date="01.01.2025",
            )
        assert "non-empty" in str(exc_info.value).lower()

    def test_empty_harness_name_rejected(self):
        """Test that empty harness names are rejected."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test suite",
                benchmark_list=[{"bench1": ["harness1", "", "harness2"]}],
                release_date="01.01.2025",
            )
        assert "empty" in str(exc_info.value).lower()

    def test_multiple_keys_in_dict_rejected(self):
        """Test that dict with multiple keys is rejected."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test suite",
                benchmark_list=[{"bench1": ["harness1"], "bench2": ["harness2"]}],
                release_date="01.01.2025",
            )
        assert "exactly one" in str(exc_info.value).lower()

    def test_duplicate_benchmarks_mixed_format(self):
        """Test that duplicate benchmarks are detected in mixed format."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test suite",
                benchmark_list=[
                    "bench1",
                    {"bench1": ["harness1"]},  # Duplicate
                ],
                release_date="01.01.2025",
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
        assert any(
            "release date" in e.message.lower() or "format" in e.message.lower()
            for e in result.errors
        )

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
        """Test validation of docs/experiment-config-example.yaml."""
        # Use Path to construct path relative to test file
        test_dir = Path(__file__).parent
        exp_path = test_dir / "assets" / "experiment-config-example.yaml"
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
experiment: test
trials: 1
mode: delta
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - test-crs
benchmarks:
  - test-bench
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


class TestProjectConfig:
    """Tests for ProjectConfig schema."""

    def test_project_config_minimal(self):
        """Test minimal valid project config."""
        config = ProjectConfig(language="c")

        assert config.language == "c"
        assert config.rts_mode is None
        assert config.inc_build is True  # defaults to True
        assert config.get_normalized_language() == "C/C++"

    def test_project_config_full(self):
        """Test full project config with all fields."""
        config = ProjectConfig(
            homepage="https://example.com",
            language="jvm",
            main_repo="git@github.com:example/repo.git",
            sanitizers=["address", "undefined"],
            fuzzing_engines=["libfuzzer"],
            architectures=["x86_64"],
            rts_mode=RtsMode.JCGEKS,
            inc_build=True,
        )

        assert config.language == "jvm"
        assert config.rts_mode == RtsMode.JCGEKS
        assert config.inc_build is True
        assert config.get_normalized_language() == "Java"

    def test_project_config_rts_modes(self):
        """Test all RTS mode values."""
        for mode in [RtsMode.JCGEKS, RtsMode.OPENCLOVER, RtsMode.BINARY_RTS]:
            config = ProjectConfig(language="jvm", rts_mode=mode)
            assert config.rts_mode == mode
            assert config.rts_mode.value in ["jcgeks", "openclover", "binary_rts"]

    def test_project_config_language_normalization(self):
        """Test language normalization."""
        test_cases = [
            ("c", "C/C++"),
            ("c++", "C/C++"),
            ("C", "C/C++"),
            ("jvm", "Java"),
            ("JVM", "Java"),
            ("go", "Go"),
            ("rust", "Rust"),
        ]
        for lang, expected in test_cases:
            config = ProjectConfig(language=lang)
            assert config.get_normalized_language() == expected

    def test_project_config_invalid_sanitizer(self):
        """Test invalid sanitizer is rejected."""
        with pytest.raises(PydanticValidationError):
            ProjectConfig(language="c", sanitizers=["invalid"])

    def test_project_config_invalid_fuzzing_engine(self):
        """Test invalid fuzzing engine is rejected."""
        with pytest.raises(PydanticValidationError):
            ProjectConfig(language="c", fuzzing_engines=["invalid"])

    def test_project_config_extra_fields_ignored(self):
        """Test that extra fields from standard OSS-Fuzz project.yaml are ignored."""
        # Standard OSS-Fuzz fields that we don't model
        config = ProjectConfig(
            language="c",
            homepage="https://example.com",
            vendor="google",  # extra field
            primary_contact="test@example.com",  # extra field
            auto_ccs=["cc@example.com"],  # extra field
        )

        assert config.language == "c"
        assert config.homepage == "https://example.com"
        # Extra fields should be silently ignored, not cause errors

    def test_project_config_without_crsbench_extensions(self):
        """Test standard OSS-Fuzz project.yaml without CRSBench extensions."""
        # Simulating a standard OSS-Fuzz project.yaml
        config = ProjectConfig(
            homepage="https://curl.haxx.se/",
            language="c",
            sanitizers=["address", "undefined", "memory"],
            fuzzing_engines=["libfuzzer"],
            architectures=["x86_64"],
            main_repo="git@github.com:example/curl.git",
        )

        # CRSBench extensions should have defaults
        assert config.rts_mode is None
        assert config.inc_build is True  # defaults to True

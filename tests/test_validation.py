"""Tests for the validation module."""

from pathlib import Path

import pytest
import yaml
from crsbench.validation import (
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
    ResourceConfig,
    RtsMode,
    Vulnerability,
    WorkerConfig,
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
            vuln_keyword="cpv_0",
            difficulty_level=3,
            povs=[
                POV(id="pov_0", sanitizer="address"),
                POV(id="pov_1", sanitizer="undefined"),
            ],
        )
        assert vuln.vuln_keyword == "cpv_0"
        assert vuln.difficulty_level == 3
        assert len(vuln.povs) == 2

    def test_vulnerability_without_difficulty(self):
        """Test vulnerability without difficulty level (optional)."""
        vuln = Vulnerability(
            vuln_keyword="cpv_1", povs=[POV(id="pov_0", sanitizer="memory")]
        )
        assert vuln.difficulty_level is None

    def test_vulnerability_duplicate_pov_ids(self):
        """Test vulnerability with duplicate POV IDs."""
        with pytest.raises(PydanticValidationError) as exc_info:
            Vulnerability(
                vuln_keyword="cpv_0",
                povs=[
                    POV(id="pov_0", sanitizer="address"),
                    POV(id="pov_0", sanitizer="undefined"),  # Duplicate
                ],
            )
        assert "duplicate" in str(exc_info.value).lower()

    def test_vulnerability_empty_povs(self):
        """Test vulnerability with no POVs."""
        with pytest.raises(PydanticValidationError):
            Vulnerability(vuln_keyword="cpv_0", povs=[])

    def test_vulnerability_with_patch_superset(self):
        """Test vulnerability with valid patch_superset."""
        vuln = Vulnerability(
            vuln_keyword="cpv_1",
            patch_superset="cpv_7",
            povs=[POV(id="pov_0", sanitizer="address")],
        )
        assert vuln.vuln_keyword == "cpv_1"
        assert vuln.patch_superset == "cpv_7"


class TestResourceConfigModel:
    """Test ResourceConfig defaults and validation."""

    def test_resource_config_defaults_are_unset(self):
        """Trial resource fallbacks should default to None."""
        config = ResourceConfig()
        assert config.cores_per_trial is None
        assert config.memory_per_trial is None


class TestWorkerConfigModel:
    """Test WorkerConfig defaults and validation."""

    def test_worker_config_defaults_do_not_impose_concurrency_or_cpu_width(self):
        """Worker jobs/CPU width should default to unset."""
        config = WorkerConfig()
        assert config.jobs is None
        assert config.cores_per_job is None


class TestVulnerabilityPatchSupersetModel:
    """Test Vulnerability patch_superset normalization and validation."""

    def test_vulnerability_without_patch_superset(self):
        """Test vulnerability without patch_superset (defaults to None)."""
        vuln = Vulnerability(
            vuln_keyword="cpv_2",
            povs=[POV(id="pov_0", sanitizer="address")],
        )
        assert vuln.patch_superset is None

    def test_vulnerability_invalid_patch_superset_format(self):
        """Test vulnerability with invalid patch_superset format."""
        with pytest.raises(PydanticValidationError) as exc_info:
            Vulnerability(
                vuln_keyword="cpv_1",
                patch_superset="invalid_format",
                povs=[POV(id="pov_0", sanitizer="address")],
            )
        assert (
            "cpv_n" in str(exc_info.value).lower()
            or "pattern" in str(exc_info.value).lower()
        )

    def test_vulnerability_empty_patch_superset(self):
        """Test vulnerability with empty patch_superset (converts to None)."""
        vuln = Vulnerability(
            vuln_keyword="cpv_1",
            patch_superset="",
            povs=[POV(id="pov_0", sanitizer="address")],
        )
        assert vuln.patch_superset is None


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

    @pytest.mark.parametrize(
        "invalid_name",
        ["", "   ", "../escape", "nested/name", "/abs/harness"],
    )
    def test_harness_rejects_invalid_name_segments(self, invalid_name: str):
        """Harness name must be a safe layout segment."""
        with pytest.raises(PydanticValidationError):
            HarnessFile(name=invalid_name, path="/src/project/test/harness.c")


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
      - vuln_keyword: "cpv_0"
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
      - vuln_keyword: "cpv_0"
        difficulty_level: 3
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"
          - id: "pov_1"
            sanitizer: "undefined"
            error_token: "runtime error: index out of bounds"
      - vuln_keyword: "cpv_1"
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
      - vuln_keyword: "cpv_0"
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
      - vuln_keyword: "cpv_0"
        povs:
          - id: "pov_0"
            sanitizer: "address"
          - id: "pov_1"
            sanitizer: "undefined"
      - vuln_keyword: "cpv_1"
        povs:
          - id: "pov_0"
            sanitizer: "memory"
  - name: "h2"
    path: "/src/h2.c"
    vulns:
      - vuln_keyword: "cpv_2"
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
      - vuln_keyword: "cpv_0"
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
        """Test validation of docs/meta-example.yaml schema.

        Uses validate_benchmark_from_string to test schema validation without
        requiring ref.diff file (which is checked by validate_benchmark).
        """
        # Use Path to construct path relative to test file
        test_dir = Path(__file__).parent
        meta_path = test_dir / "assets" / "meta-example.yaml"
        with meta_path.open() as f:
            yaml_content = f.read()
        result = validate_benchmark_from_string(yaml_content)

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
    """Test ExperimentConfig schema validation (strict contract)."""

    @staticmethod
    def _base_kwargs() -> dict:
        return {
            "experiment": "test",
            "trials": 1,
            "mode": EvaluationMode.DELTA,
            "max_total_time": 20000,
            "inputs": {"pov": {"enabled": True, "max_variants_per_cpv": 1}},
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/rep",
            "benchmarks": ["test-bench"],
            "crs_compose": {"test-crs": {"num_cores": 1}},
        }

    def test_experiment_config_valid(self):
        config = ExperimentConfig(**self._base_kwargs())
        assert config.trials == 1
        assert config.max_total_time == 20000
        assert config.get_crs_registry_ids() == ["test-crs"]

    def test_grouped_experiment_runtime_storage_normalize(self):
        config = ExperimentConfig.model_validate(
            {
                "experiment": {
                    "name": "test-grouped",
                    "task": "bugfixing",
                    "benchmark_suite": "afc-final",
                    "sanitizers": ["address", "undefined"],
                    "mode": "full",
                    "only_cpv_harnesses": False,
                },
                "runtime": {
                    "trials": 2,
                    "max_total_time": 25000,
                    "inputs": {"pov": {"max_variants_per_cpv": 2}},
                    "redis": {"host": "localhost"},
                    "skip_litellm": True,
                    "litellm": {
                        "mode": "external",
                        "tracking_enabled": True,
                        "cost_budget": 25.0,
                    },
                },
                "storage": {
                    "experiment_filestore": "/tmp/experiment-data",
                    "report_filestore": "/tmp/report-data",
                },
                "crs_compose": {"test-crs": {"num_cores": 1}},
            }
        )
        assert config.experiment == "test-grouped"
        assert config.task == "bugfixing"
        assert config.benchmark_suite == "afc-final"
        assert [s.value for s in config.sanitizers] == ["address", "undefined"]
        assert config.mode == EvaluationMode.FULL
        assert config.only_cpv_harnesses is False
        assert config.trials == 2
        assert config.inputs.pov.max_variants_per_cpv == 2
        assert config.redis_host == "localhost"
        assert config.skip_litellm is True
        assert config.litellm_mode is None
        assert config.llm_tracking_enabled is False
        assert config.litellm_cost_budget == 25.0

    def test_grouped_runtime_redis_precedence_over_top_level(self):
        config = ExperimentConfig.model_validate(
            {
                "experiment": {
                    "name": "test-grouped-redis",
                    "task": "bugfinding",
                    "benchmark_suite": "sanity",
                    "mode": "delta",
                },
                "redis_host": "top-level-redis:6379",
                "runtime": {
                    "trials": 1,
                    "max_total_time": 20000,
                    "inputs": {"pov": {"enabled": False, "max_variants_per_cpv": 1}},
                    "redis": {"host": "runtime-redis:6380"},
                },
                "storage": {
                    "experiment_filestore": "/tmp/experiment-data",
                    "report_filestore": "/tmp/report-data",
                },
                "crs_compose": {"test-crs": {"num_cores": 1}},
            }
        )
        assert config.redis_host == "runtime-redis:6380"

    def test_grouped_conflict_rejected(self):
        with pytest.raises(PydanticValidationError, match="Conflicting values"):
            ExperimentConfig.model_validate(
                {
                    "experiment": {"name": "test", "benchmark_suite": "afc-final"},
                    "benchmark_suite": "sanity",
                    "runtime": {
                        "trials": 1,
                        "max_total_time": 20000,
                        "inputs": {"pov": {"max_variants_per_cpv": 1}},
                    },
                    "storage": {
                        "experiment_filestore": "/tmp/exp",
                        "report_filestore": "/tmp/rep",
                    },
                    "crs_compose": {"test-crs": {"num_cores": 1}},
                }
            )

    def test_inputs_defaults_to_disabled_when_missing(self):
        data = self._base_kwargs()
        data.pop("inputs")
        config = ExperimentConfig(**data)
        assert config.inputs.pov.enabled is False
        assert config.inputs.sarif.enabled is False
        assert config.inputs.seed.enabled is False
        assert config.inputs.diff.enabled is False

    def test_crs_compose_required(self):
        data = self._base_kwargs()
        data.pop("crs_compose")
        with pytest.raises(PydanticValidationError, match="crs_compose is required"):
            ExperimentConfig(**data)

    def test_legacy_keys_rejected(self):
        data = self._base_kwargs()
        data["adapter"] = "oss-crs"
        with pytest.raises(
            PydanticValidationError, match="Extra inputs are not permitted"
        ):
            ExperimentConfig(**data)

        data = self._base_kwargs()
        data["difficulty_level"] = 1
        with pytest.raises(
            PydanticValidationError, match="Extra inputs are not permitted"
        ):
            ExperimentConfig(**data)

        data = self._base_kwargs()
        data["oss_crs_registry"] = ["test-crs"]
        with pytest.raises(
            PydanticValidationError, match="Extra inputs are not permitted"
        ):
            ExperimentConfig(**data)

        data = self._base_kwargs()
        data["crses"] = ["test-crs"]
        with pytest.raises(
            PydanticValidationError, match="Extra inputs are not permitted"
        ):
            ExperimentConfig(**data)

        data = self._base_kwargs()
        data["resources"] = {"litellm": {"cost_budget": 50.0}}
        with pytest.raises(
            PydanticValidationError, match="Extra inputs are not permitted"
        ):
            ExperimentConfig(**data)

    def test_inputs_presence_based(self):
        data = self._base_kwargs()
        data["task"] = "bugfixing"
        data["inputs"] = {
            "pov": {"max_variants_per_cpv": 2},
            "sarif": {"level": 1},
        }
        config = ExperimentConfig(**data)
        assert config.inputs.pov.enabled is True
        assert config.inputs.sarif.enabled is True
        assert config.inputs.sarif.level == 1
        assert config.inputs.seed.enabled is False

    def test_inputs_sarif_enabled_requires_level(self):
        data = self._base_kwargs()
        data["inputs"] = {
            "pov": {"enabled": True, "max_variants_per_cpv": 1},
            "sarif": {"enabled": True},
        }
        with pytest.raises(
            PydanticValidationError, match="inputs\\.sarif requires level"
        ):
            ExperimentConfig(**data)

    def test_bugfinding_rejects_pov_input(self):
        data = self._base_kwargs()
        data["task"] = "bugfinding"
        data["inputs"] = {
            "pov": {"enabled": True, "max_variants_per_cpv": 1},
        }
        with pytest.raises(
            PydanticValidationError, match="incompatible with task='bugfinding'"
        ):
            ExperimentConfig(**data)

    def test_bugfinding_accepts_non_pov_runtime_inputs(self):
        data = self._base_kwargs()
        data["task"] = "bugfinding"
        data["inputs"] = {
            "pov": {"enabled": False},
            "sarif": {"enabled": True, "level": 1},
            "seed": {"enabled": True},
            "diff": {"enabled": True},
        }
        config = ExperimentConfig(**data)
        assert config.inputs.pov.enabled is False
        assert config.inputs.sarif.enabled is True
        assert config.inputs.seed.enabled is True
        assert config.inputs.diff.enabled is True

    def test_worker_shared_cpus_rejected(self):
        data = self._base_kwargs()
        data["worker"] = {"shared_cpus": "0-3"}
        with pytest.raises(
            PydanticValidationError, match="Extra inputs are not permitted"
        ):
            ExperimentConfig(**data)

    def test_cloud_gce_valid(self):
        data = self._base_kwargs()
        data["cloud"] = {
            "gce": {
                "project": "test-project",
                "zone": "us-central1-a",
                "worker_count": 2,
                "machine_type": "e2-standard-4",
                "boot_disk_size_gb": 100,
                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                "owner_label": "team-crs",
                "ssh_via_iap": True,
                "readiness_timeout_sec": 1200,
            }
        }
        config = ExperimentConfig(**data)
        assert config.cloud is not None
        assert config.cloud.gce.project == "test-project"
        assert config.cloud.gce.zone == "us-central1-a"
        assert config.cloud.gce.region is None
        assert config.cloud.gce.use_os_login is True
        assert config.cloud.gce.ssh_via_iap is True

    def test_cloud_gce_requires_exactly_one_location(self):
        data = self._base_kwargs()
        data["cloud"] = {
            "gce": {
                "project": "test-project",
                "zone": "us-central1-a",
                "region": "us-central1",
                "worker_count": 2,
                "machine_type": "e2-standard-4",
                "boot_disk_size_gb": 100,
                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                "owner_label": "team-crs",
            }
        }
        with pytest.raises(
            PydanticValidationError, match="exactly one of 'zone' or 'region'"
        ):
            ExperimentConfig(**data)

    def test_cloud_gce_rejects_image_and_instance_template(self):
        data = self._base_kwargs()
        data["cloud"] = {
            "gce": {
                "project": "test-project",
                "zone": "us-central1-a",
                "worker_count": 2,
                "machine_type": "e2-standard-4",
                "boot_disk_size_gb": 100,
                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "instance_template": "global/instanceTemplates/crsbench-worker",
                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                "owner_label": "team-crs",
            }
        }
        with pytest.raises(
            PydanticValidationError,
            match="exactly one of 'image' or 'instance_template'",
        ):
            ExperimentConfig(**data)

    def test_cloud_gce_rejects_unsupported_access_mode(self):
        data = self._base_kwargs()
        data["cloud"] = {
            "gce": {
                "project": "test-project",
                "zone": "us-central1-a",
                "worker_count": 2,
                "machine_type": "e2-standard-4",
                "boot_disk_size_gb": 100,
                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                "owner_label": "team-crs",
                "use_os_login": False,
            }
        }
        with pytest.raises(
            PydanticValidationError, match="use_os_login must remain true"
        ):
            ExperimentConfig(**data)

    def test_benchmarks_nested_harness_cpvs(self):
        data = self._base_kwargs()
        data["benchmarks"] = [
            {"bench1": {"harness1": ["cpv_0", "cpv_1"], "harness2": ["cpv_0"]}}
        ]
        config = ExperimentConfig(**data)
        entries = config.get_benchmark_entries()
        assert len(entries) == 1
        assert entries[0].name == "bench1"
        assert entries[0].harnesses == ["harness1", "harness2"]
        assert entries[0].harness_cpvs == {
            "harness1": ["cpv_0", "cpv_1"],
            "harness2": ["cpv_0"],
        }

    def test_benchmarks_nested_empty_cpv_rejected(self):
        data = self._base_kwargs()
        data["benchmarks"] = [{"bench1": {"harness1": []}}]
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(**data)
        assert "cpv list" in str(exc_info.value).lower()

    def test_invalid_trials(self):
        data = self._base_kwargs()
        data["trials"] = 0
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(**data)
        assert "trials" in str(exc_info.value).lower()

    def test_invalid_max_total_time(self):
        data = self._base_kwargs()
        data["max_total_time"] = 0
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(**data)
        assert "max_total_time" in str(exc_info.value).lower()

    def test_experiment_config_with_redis_host(self):
        data = self._base_kwargs()
        data["redis_host"] = "localhost"
        config = ExperimentConfig(**data)
        assert config.redis_host == "localhost"

    def test_experiment_config_redis_host_none_string(self):
        data = self._base_kwargs()
        data["redis_host"] = "none"
        config = ExperimentConfig(**data)
        assert config.redis_host is None

    def test_benchmarks_root_custom_and_blank_defaults(self):
        data = self._base_kwargs()
        data["benchmarks_root"] = "/custom/benchmarks"
        data["benchmark_suites_root"] = "   "
        config = ExperimentConfig(**data)
        assert config.benchmarks_root == Path("/custom/benchmarks")
        assert config.benchmark_suites_root == Path("benchmark-suites").resolve()

    def test_runtime_inc_build_group_maps_to_top_level_fields(self):
        config = ExperimentConfig(
            experiment={
                "name": "test",
                "task": "bugfixing",
                "benchmarks": ["test-bench"],
                "sanitizers": ["address"],
                "mode": "delta",
            },
            runtime={
                "trials": 1,
                "max_total_time": 20000,
                "build_timeout": 300,
                "run_timeout": 300,
                "verify_timeout": 300,
                "inputs": {"pov": {"max_variants_per_cpv": 1}},
                "inc_build": {
                    "policy": "build_only",
                    "registry": "local",
                    "max_pull_bytes": 123456,
                    "pull_timeout_sec": 77,
                },
                "litellm": {"mode": None, "tracking_enabled": False},
            },
            storage={
                "experiment_filestore": "/tmp/exp",
                "report_filestore": "/tmp/rep",
            },
            crs_compose={"test-crs": {"num_cores": 1}},
        )
        assert config.inc_image_policy == "build_only"
        assert config.inc_image_registry == "local"
        assert config.inc_image_max_pull_bytes == 123456
        assert config.inc_image_pull_timeout_sec == 77

    def test_runtime_inc_build_conflict_with_top_level_rejected(self):
        with pytest.raises(PydanticValidationError, match="Conflicting values"):
            ExperimentConfig(
                experiment={
                    "name": "test",
                    "task": "bugfixing",
                    "benchmarks": ["test-bench"],
                    "sanitizers": ["address"],
                    "mode": "delta",
                },
                runtime={
                    "trials": 1,
                    "max_total_time": 20000,
                    "build_timeout": 300,
                    "run_timeout": 300,
                    "verify_timeout": 300,
                    "inputs": {"pov": {"max_variants_per_cpv": 1}},
                    "inc_build": {"policy": "build_only"},
                    "litellm": {"mode": None, "tracking_enabled": False},
                },
                storage={
                    "experiment_filestore": "/tmp/exp",
                    "report_filestore": "/tmp/rep",
                },
                inc_image_policy="pull_only",
                crs_compose={"test-crs": {"num_cores": 1}},
            )

    def test_model_dump_and_only_cpv_harnesses(self):
        data = self._base_kwargs()
        data["only_cpv_harnesses"] = False
        config = ExperimentConfig(**data)
        config_dict = config.model_dump()
        assert config_dict["mode"] == "delta"
        assert config_dict["max_total_time"] == 20000
        assert config_dict["only_cpv_harnesses"] is False
        assert "difficulty_level" not in config_dict

    def test_max_total_time_validation_failure(self):
        data = self._base_kwargs()
        data["max_total_time"] = 18000
        with pytest.raises(PydanticValidationError) as exc_info:
            ExperimentConfig(**data)
        error_msg = str(exc_info.value)
        assert "max_total_time" in error_msg
        assert "build_timeout + run_timeout + verify_timeout" in error_msg

    def test_crs_compose_rejects_legacy_nested_services(self):
        data = self._base_kwargs()
        data["crs_compose"] = {
            "oss_crs_infra": {"num_cores": 1},
            "crs_services": {"test-crs": {"num_cores": 1}},
        }
        with pytest.raises(
            PydanticValidationError, match="Legacy crs_compose\\.crs_services"
        ):
            ExperimentConfig(**data)

    def test_crs_compose_rejects_infra_additional_env(self):
        data = self._base_kwargs()
        data["crs_compose"] = {
            "oss_crs_infra": {
                "num_cores": 1,
                "additional_env": {"FOO": "BAR"},
            },
            "test-crs": {"num_cores": 1},
        }
        with pytest.raises(PydanticValidationError, match="extra_forbidden"):
            ExperimentConfig(**data)

    def test_crs_compose_infra_modes(self):
        # shared mode valid
        data = self._base_kwargs()
        data["crs_compose"] = {
            "oss_crs_infra": {"shared": True},
            "test-crs": {"num_cores": 1},
        }
        config = ExperimentConfig(**data)
        assert config.crs_compose is not None
        assert config.crs_compose.oss_crs_infra.shared is True

        # both shared + num_cores invalid
        bad = self._base_kwargs()
        bad["crs_compose"] = {
            "oss_crs_infra": {"shared": True, "num_cores": 1},
            "test-crs": {"num_cores": 1},
        }
        with pytest.raises(PydanticValidationError, match="exactly one CPU mode"):
            ExperimentConfig(**bad)

        # neither shared nor num_cores invalid
        bad2 = self._base_kwargs()
        bad2["crs_compose"] = {
            "oss_crs_infra": {"shared": False},
            "test-crs": {"num_cores": 1},
        }
        with pytest.raises(PydanticValidationError, match="exactly one CPU mode"):
            ExperimentConfig(**bad2)

    def test_litellm_mode_explicit_missing_runtime_env_allowed_at_schema_stage(
        self, monkeypatch
    ):
        monkeypatch.delenv("CRSBENCH_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("CRSBENCH_LLM_UPSTREAM_BASE_URL", raising=False)
        monkeypatch.delenv("CRSBENCH_LLM_UPSTREAM_API_KEY", raising=False)
        monkeypatch.delenv("CRSBENCH_LLM_UPSTREAM_MASTER_KEY", raising=False)

        data = self._base_kwargs()
        data["litellm_mode"] = "external"
        config = ExperimentConfig(**data)
        assert config.litellm_mode == "external"

    def test_skip_litellm_disables_runtime_interface_validation(self, monkeypatch):
        monkeypatch.delenv("CRSBENCH_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("CRSBENCH_LLM_UPSTREAM_BASE_URL", raising=False)
        monkeypatch.delenv("CRSBENCH_LLM_UPSTREAM_API_KEY", raising=False)
        monkeypatch.delenv("CRSBENCH_LLM_UPSTREAM_MASTER_KEY", raising=False)

        data = self._base_kwargs()
        data["litellm_mode"] = "external"
        data["skip_litellm"] = True
        config = ExperimentConfig(**data)
        assert config.litellm_mode is None
        assert config.llm_tracking_enabled is False

    def test_self_hosted_mode_not_implemented(self):
        data = self._base_kwargs()
        data["litellm_mode"] = "self_hosted"
        with pytest.raises(PydanticValidationError, match="not implemented yet"):
            ExperimentConfig(**data)


class TestExperimentConfigValidation:
    """Test experiment config validation with YAML (strict contract)."""

    def test_validate_experiment_valid(self):
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
benchmarks:
  - test-bench
crs_compose:
  test-crs:
    num_cores: 1
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is True
        assert result.error_count == 0
        assert result.metadata["trials"] == 1
        assert result.metadata["max_total_time"] == 86400

    def test_validate_experiment_missing_inputs_uses_defaults(self):
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
benchmarks:
  - test-bench
crs_compose:
  test-crs:
    num_cores: 1
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is True
        assert result.error_count == 0

    def test_validate_experiment_legacy_keys_rejected(self):
        yaml_content = """
experiment: test
trials: 1
mode: delta
adapter: oss-crs
max_total_time: 86400
difficulty_level: 1
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
oss_crs_registry:
  - test-crs
benchmarks:
  - test-bench
crs_compose:
  test-crs:
    num_cores: 1
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is False
        error_messages = " ".join(e.message for e in result.errors)
        assert "Unknown field 'adapter'" in error_messages
        assert "Unknown field 'difficulty_level'" in error_messages
        assert "Unknown field 'oss_crs_registry'" in error_messages

    def test_validate_experiment_negative_trials(self):
        yaml_content = """
experiment: test
trials: -1
mode: delta
max_total_time: 86400
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/exp
  report_filestore: /tmp/crsbench/rep
benchmarks:
  - test-bench
crs_compose:
  test-crs:
    num_cores: 1
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is False
        assert any("trials" in e.message.lower() for e in result.errors)

    def test_validate_experiment_with_redis_host(self):
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
benchmarks:
  - test-bench
redis_host: queue-server
crs_compose:
  test-crs:
    num_cores: 1
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is True
        assert result.metadata.get("redis_host") == "queue-server"

    def test_validate_experiment_redis_none_for_local_mode(self):
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
benchmarks:
  - test-bench
redis_host: none
crs_compose:
  test-crs:
    num_cores: 1
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is True
        assert result.metadata.get("redis_host") is None

    def test_validate_experiment_typo_nested_worker_field(self):
        yaml_content = """
experiment: test
trials: 1
mode: delta
max_total_time: 86400
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
benchmarks:
  - test-bench
crs_compose:
  test-crs:
    num_cores: 1
worker:
  job: 4
"""
        result = validate_experiment_config_from_string(yaml_content)
        assert result.is_valid is False
        error_messages = " ".join(e.message for e in result.errors)
        assert "job" in error_messages.lower()


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

    def test_nested_harness_cpv_format(self):
        """Test nested dict format (harness -> cpv list)."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[
                {"bench1": {"harness1": ["cpv_0", "cpv_1"], "harness2": ["cpv_0"]}}
            ],
            release_date="01.01.2025",
        )
        assert isinstance(config.benchmark_list[0], dict)
        assert config.benchmark_list[0]["bench1"]["harness1"] == ["cpv_0", "cpv_1"]
        assert config.benchmark_list[0]["bench1"]["harness2"] == ["cpv_0"]

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
        assert entries[0].harness_cpvs is None

    def test_get_benchmark_entries_nested_harness_cpvs(self):
        """Test get_benchmark_entries() with nested harness->cpv format."""
        config = BenchmarkSuiteConfig(
            Name="test-suite",
            Description="Test suite",
            benchmark_list=[
                {"bench1": {"harness1": ["cpv_0", "cpv_1"], "harness2": ["cpv_0"]}}
            ],
            release_date="01.01.2025",
        )
        entries = config.get_benchmark_entries()
        assert len(entries) == 1
        assert entries[0].name == "bench1"
        assert entries[0].harnesses == ["harness1", "harness2"]
        assert entries[0].harness_cpvs == {
            "harness1": ["cpv_0", "cpv_1"],
            "harness2": ["cpv_0"],
        }

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

    def test_empty_cpv_list_rejected(self):
        """Test that empty CPV list is rejected."""
        with pytest.raises(PydanticValidationError) as exc_info:
            BenchmarkSuiteConfig(
                Name="test-suite",
                Description="Test suite",
                benchmark_list=[{"bench1": {"harness1": []}}],
                release_date="01.01.2025",
            )
        assert "cpv list" in str(exc_info.value).lower()

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

    def test_validate_benchmark_suite_typo_field(self):
        """Typo'd field in benchmark suite config should be caught."""
        yaml_content = """
Name: test-suite
Description: A test suite
Release date: 01.01.2025
benchmark_lis:
  - bench-01
"""
        result = validate_benchmark_suite_from_string(yaml_content)

        assert result.is_valid is False
        error_messages = " ".join(e.message for e in result.errors)
        assert "benchmark_lis" in error_messages
        assert "did you mean" in error_messages.lower()
        assert "benchmark_list" in error_messages.lower()


# ============================================================================
# Integration Tests for All Config Types
# ============================================================================


class TestIntegrationAllConfigs:
    """Integration tests with all configuration types."""

    def test_validate_experiment_example_yaml(self, monkeypatch):
        """Test validation of tests/assets/experiment-config-example.yaml."""
        monkeypatch.setenv("CRSBENCH_LLM_BASE_URL", "http://litellm:4000")
        monkeypatch.setenv("CRSBENCH_LLM_UPSTREAM_BASE_URL", "http://litellm:4000")
        monkeypatch.setenv("CRSBENCH_LLM_UPSTREAM_MASTER_KEY", "sk-test")
        monkeypatch.setenv("CRSBENCH_LLM_UPSTREAM_API_KEY", "sk-test")
        # Use Path to construct path relative to test file
        test_dir = Path(__file__).parent
        exp_path = test_dir / "assets" / "experiment-config-example.yaml"
        result = validate_experiment_config(exp_path)

        assert result.is_valid is True
        assert result.metadata["trials"] == 3
        assert result.metadata["max_total_time"] == 28800
        assert "difficulty_level" not in result.metadata

    def test_validate_distributed_source_of_truth_example_yaml(self, monkeypatch):
        """Validate docs distributed example as the canonical grouped config."""
        monkeypatch.setenv("CRSBENCH_LLM_BASE_URL", "http://litellm:4000")
        monkeypatch.setenv("CRSBENCH_LLM_UPSTREAM_BASE_URL", "http://litellm:4000")
        monkeypatch.setenv("CRSBENCH_LLM_UPSTREAM_MASTER_KEY", "sk-test")
        monkeypatch.setenv("CRSBENCH_LLM_UPSTREAM_API_KEY", "sk-test")

        repo_root = Path(__file__).resolve().parents[1]
        exp_path = repo_root / "docs" / "experiment-config-distributed-example.yaml"
        result = validate_experiment_config(exp_path)
        config = ExperimentConfig(**yaml.safe_load(exp_path.read_text()))

        assert result.is_valid is True
        assert config.task == "bugfixing"
        assert config.mode == EvaluationMode.DELTA
        assert config.benchmark_suite == "afc-final"
        assert config.trials == 3
        assert config.max_total_time == 28800
        assert config.redis_host == "localhost:6379"
        assert config.cloud is not None
        assert config.cloud.gce.project == "example-project"
        assert config.cloud.gce.zone == "us-central1-a"
        assert config.cloud.gce.use_os_login is True

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
inputs:
  pov:
    enabled: true
    max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/exp
  report_filestore: /tmp/crsbench/rep
benchmarks:
  - test-bench
crs_compose:
  test-crs:
    num_cores: 1
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
        for mode in [
            RtsMode.NONE,
            RtsMode.JCGEKS,
            RtsMode.OPENCLOVER,
            RtsMode.BINARYRTS,
        ]:
            config = ProjectConfig(language="jvm", rts_mode=mode)
            assert config.rts_mode == mode
            assert config.rts_mode.value in [
                "none",
                "jcgeks",
                "openclover",
                "binaryrts",
            ]

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

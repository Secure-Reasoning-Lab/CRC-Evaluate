"""Unit tests for the CRS adapter module.

Tests OssCrsAdapter (both modes), create_adapter helper, single-value
AdapterType enum, ExperimentConfig schema integration with the adapter field,
and BenchmarkRunner adapter-based branching via _crs_type property.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from crsbench.evaluation.adapter import OssCrsAdapter, create_adapter
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.validation.schemas import AdapterType

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FACTORY_ARGS = {
    "crs_config_name": "test-crs",
    "oss_fuzz_path": Path("/tmp/fake/oss-fuzz"),
    "registry_dir": Path("/tmp/fake/registry"),
    "benchmarks_root": Path("/tmp/fake/benchmarks"),
    "crs_configs_dir": Path("/tmp/fake/configs"),
}


# ---------------------------------------------------------------------------
# AdapterType enum tests
# ---------------------------------------------------------------------------


class TestAdapterType:
    """Tests for the single-value AdapterType enum."""

    def test_has_one_value(self) -> None:
        assert len(AdapterType) == 1

    def test_oss_crs_value(self) -> None:
        assert AdapterType.OSS_CRS.value == "oss-crs"

    def test_is_str_subclass(self) -> None:
        assert isinstance(AdapterType.OSS_CRS, str)
        assert AdapterType.OSS_CRS == "oss-crs"


# ---------------------------------------------------------------------------
# OssCrsAdapter tests
# ---------------------------------------------------------------------------


class TestOssCrsAdapter:
    """Tests for OssCrsAdapter with both modes."""

    def test_mode_bug_finding(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        assert adapter.mode == "bug-finding"

    def test_mode_bug_fixing(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-fixing")
        assert adapter.mode == "bug-fixing"

    def test_built_projects_initially_empty(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        assert adapter.built_projects == set()

    def test_built_projects_mutable_via_internal_set(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        adapter._built_projects.add("project-a")
        assert "project-a" in adapter.built_projects

    def test_configure_sets_timeouts(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        adapter.configure(
            {
                "build_timeout": 1800,
                "run_timeout": 3600,
                "docker_registry": "ghcr.io/test",
            }
        )
        assert adapter._build_timeout == 1800
        assert adapter._run_timeout == 3600
        assert adapter._docker_registry == "ghcr.io/test"

    def test_configure_sets_compose_fields(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        adapter.configure(
            {
                "docker_registry": "ghcr.io/test",
                "oss_crs_cmd": "/usr/local/bin/oss-crs",
                "oss_crs_infra_cpuset": "0-7",
                "oss_crs_infra_memory": "16G",
            }
        )
        assert adapter._oss_crs_cmd == "/usr/local/bin/oss-crs"
        assert adapter._oss_crs_infra_cpuset == "0-7"
        assert adapter._oss_crs_infra_memory == "16G"

    def test_run_requires_build_first(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        harness = MagicMock()
        harness.name = "test_harness"
        with pytest.raises(RuntimeError, match="build"):
            adapter.run(Path("/tmp"), harness, Path("/tmp"))

    def test_configure_sets_external_litellm(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-fixing")
        adapter.configure(
            {
                "external_litellm": True,
                "litellm_url": "http://litellm:4000",
                "litellm_api_key": "sk-test-key",
            }
        )
        assert adapter._external_litellm is True
        assert adapter._litellm_url == "http://litellm:4000"
        assert adapter._litellm_api_key == "sk-test-key"


# ---------------------------------------------------------------------------
# create_adapter tests
# ---------------------------------------------------------------------------


class TestCreateAdapter:
    """Tests for the create_adapter() helper function."""

    def test_returns_oss_crs_adapter(self) -> None:
        config = MagicMock()
        config.litellm_mode = "passthrough"
        adapter = create_adapter(
            config=config,
            crs_config_name="test-crs",
            oss_fuzz_path=Path("/tmp/fake/oss-fuzz"),
            registry_dir=Path("/tmp/fake/registry"),
            benchmarks_root=Path("/tmp/fake/benchmarks"),
            crs_configs_dir=Path("/tmp/fake/configs"),
            mode="bug-finding",
        )
        assert isinstance(adapter, OssCrsAdapter)

    def test_mode_passed_through(self) -> None:
        config = MagicMock()
        config.litellm_mode = "passthrough"
        adapter = create_adapter(
            config=config,
            crs_config_name="test-crs",
            oss_fuzz_path=Path("/tmp/fake/oss-fuzz"),
            registry_dir=Path("/tmp/fake/registry"),
            benchmarks_root=Path("/tmp/fake/benchmarks"),
            crs_configs_dir=Path("/tmp/fake/configs"),
            mode="bug-fixing",
        )
        assert adapter.mode == "bug-fixing"

    def test_default_mode_is_bug_finding(self) -> None:
        config = MagicMock()
        config.litellm_mode = None
        adapter = create_adapter(
            config=config,
            crs_config_name="test-crs",
            oss_fuzz_path=Path("/tmp/fake/oss-fuzz"),
            registry_dir=Path("/tmp/fake/registry"),
            benchmarks_root=Path("/tmp/fake/benchmarks"),
            crs_configs_dir=Path("/tmp/fake/configs"),
        )
        assert adapter.mode == "bug-finding"


# ---------------------------------------------------------------------------
# ExperimentConfig schema integration tests
# ---------------------------------------------------------------------------


class TestAdapterSchemaIntegration:
    """Tests for adapter field on ExperimentConfig."""

    def test_valid_adapter_parses(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore=Path("/tmp/store"),
            report_filestore=Path("/tmp/report"),
            crses=["crs1"],
            benchmarks=["bench1"],
        )
        assert config.adapter == AdapterType.OSS_CRS

    def test_adapter_string_coercion(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter="oss-crs",
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore=Path("/tmp/store"),
            report_filestore=Path("/tmp/report"),
            crses=["crs1"],
            benchmarks=["bench1"],
        )
        assert config.adapter == AdapterType.OSS_CRS

    def test_adapter_defaults_to_oss_crs(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            max_total_time=86400,
            difficulty_level=1,
            experiment_filestore=Path("/tmp/store"),
            report_filestore=Path("/tmp/report"),
            crses=["crs1"],
            benchmarks=["bench1"],
        )
        assert config.adapter == AdapterType.OSS_CRS

    def test_invalid_adapter_raises_validation_error(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="adapter"):
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode="delta",
                adapter="not-a-valid-adapter",
                max_total_time=86400,
                difficulty_level=1,
                experiment_filestore=Path("/tmp/store"),
                report_filestore=Path("/tmp/report"),
                crses=["crs1"],
                benchmarks=["bench1"],
            )


# ---------------------------------------------------------------------------
# BenchmarkRunner adapter-based branching tests
# ---------------------------------------------------------------------------


class TestBenchmarkRunnerAdapterBranching:
    """Tests for BenchmarkRunner._crs_type property with required adapter."""

    def test_crs_type_returns_bug_finding(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        runner = BenchmarkRunner(adapter=adapter)
        assert runner._crs_type == "bug-finding"

    def test_crs_type_returns_bug_fixing(self) -> None:
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-fixing")
        runner = BenchmarkRunner(adapter=adapter)
        assert runner._crs_type == "bug-fixing"

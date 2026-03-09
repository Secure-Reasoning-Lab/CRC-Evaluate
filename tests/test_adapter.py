"""Unit tests for the CRS adapter module.

Tests OssCrsAdapter (both modes), create_adapter helper, single-value
AdapterType enum, ExperimentConfig strict-contract integration, and
BenchmarkRunner adapter-based branching via _crs_type property.
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
        assert adapter._infra_mem_limit == "16G"

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
                "litellm_runtime_url": "http://litellm:4000",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        assert adapter._litellm_runtime_url == "http://litellm:4000"
        assert adapter._litellm_runtime_api_key == "sk-test-key"


# ---------------------------------------------------------------------------
# create_adapter tests
# ---------------------------------------------------------------------------


class TestCreateAdapter:
    """Tests for the create_adapter() helper function."""

    def test_returns_oss_crs_adapter(self) -> None:
        config = MagicMock()
        config.litellm_mode = "external"
        adapter = create_adapter(
            config=config,
            crs_config_name="test-crs",
            oss_fuzz_path=Path("/tmp/fake/oss-fuzz"),
            registry_dir=Path("/tmp/fake/registry"),
            benchmarks_root=Path("/tmp/fake/benchmarks"),
            mode="bug-finding",
        )
        assert isinstance(adapter, OssCrsAdapter)

    def test_mode_passed_through(self) -> None:
        config = MagicMock()
        config.litellm_mode = "external"
        adapter = create_adapter(
            config=config,
            crs_config_name="test-crs",
            oss_fuzz_path=Path("/tmp/fake/oss-fuzz"),
            registry_dir=Path("/tmp/fake/registry"),
            benchmarks_root=Path("/tmp/fake/benchmarks"),
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
        )
        assert adapter.mode == "bug-finding"


# ---------------------------------------------------------------------------
# ExperimentConfig schema integration tests
# ---------------------------------------------------------------------------


class TestAdapterSchemaIntegration:
    """Tests for strict ExperimentConfig contract integration."""

    def test_valid_strict_contract_parses(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            max_total_time=86400,
            inputs={"pov": {"enabled": False}},
            experiment_filestore=Path("/tmp/store"),
            report_filestore=Path("/tmp/report"),
            benchmarks=["bench1"],
            crs_compose={
                "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
                "crs1": {"num_cores": 1, "mem_limit": "8G"},
            },
        )
        assert config.get_crs_registry_ids() == ["crs1"]

    def test_compose_roundtrip_with_shared_infra_parses(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            max_total_time=86400,
            inputs={"pov": {"enabled": False}},
            experiment_filestore=Path("/tmp/store"),
            report_filestore=Path("/tmp/report"),
            benchmarks=["bench1"],
            crs_compose={"crs1": {"num_cores": 1, "mem_limit": "8G"}},
        )
        payload = config.model_dump(mode="json")

        adapter = OssCrsAdapter(
            crs_config_name="crs1",
            oss_fuzz_path=Path("/tmp/oss-fuzz"),
            registry_dir=Path("/tmp/registry"),
            benchmarks_root=Path("/tmp/benchmarks"),
        )
        adapter.configure(payload["crs_compose"])
        assert adapter._infra_shared is True
        assert adapter._infra_num_cores == 0

    def test_adapter_field_is_rejected(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode="delta",
                adapter="oss-crs",
                max_total_time=86400,
                inputs={"pov": {"enabled": False}},
                experiment_filestore=Path("/tmp/store"),
                report_filestore=Path("/tmp/report"),
                benchmarks=["bench1"],
                crs_compose={
                    "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
                    "crs1": {"num_cores": 1, "mem_limit": "8G"},
                },
            )

    def test_legacy_oss_crs_registry_is_rejected(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
            ExperimentConfig(
                experiment="test",
                trials=1,
                mode="delta",
                max_total_time=86400,
                inputs={"pov": {"enabled": False}},
                experiment_filestore=Path("/tmp/store"),
                report_filestore=Path("/tmp/report"),
                oss_crs_registry=["crs1"],
                benchmarks=["bench1"],
                crs_compose={
                    "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
                    "crs1": {"num_cores": 1, "mem_limit": "8G"},
                },
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

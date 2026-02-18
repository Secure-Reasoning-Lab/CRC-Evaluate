"""Unit tests for CRSRunJob.

Tests the CRSRunJob class which encapsulates CRS execution with internal
periodic verification support.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.experiment.jobs import CRSRunJob


@pytest.fixture
def base_job_params(tmp_path: Path) -> dict:
    """Provide base parameters for creating CRSRunJob."""
    return {
        "crs_config_name": "test-crs",
        "benchmark_path": tmp_path / "benchmark",
        "harness_name": "test_harness",
        "trial_num": 1,
        "trial_output_dir": tmp_path / "trial-output",
        "oss_fuzz_path": tmp_path / "oss-fuzz",
        "registry_dir": tmp_path / "registry",
        "benchmarks_root": tmp_path / "benchmarks",
        "crs_configs_dir": tmp_path / "crs-configs",
    }


class TestCRSRunJobProperties:
    """Test CRSRunJob property methods."""

    def test_job_id_format(self, base_job_params: dict) -> None:
        """Verify job_id follows expected format."""
        job = CRSRunJob(**base_job_params)
        expected = f"crs-run:{base_job_params['benchmark_path'].name}:test_harness:1"
        assert job.job_id == expected

    def test_job_type_is_crs_run(self, base_job_params: dict) -> None:
        """Verify job_type is 'crs_run'."""
        job = CRSRunJob(**base_job_params)
        assert job.job_type == "crs_run"

    def test_depends_on_returns_build_job_id_when_set(
        self, base_job_params: dict
    ) -> None:
        """Verify depends_on returns build_job_id when set."""
        base_job_params["build_job_id"] = "build:test-project"
        job = CRSRunJob(**base_job_params)
        assert job.depends_on == ["build:test-project"]

    def test_depends_on_empty_when_no_build_job_id(self, base_job_params: dict) -> None:
        """When build_job_id is empty string, depends_on returns []."""
        # Default is empty string
        job = CRSRunJob(**base_job_params)
        assert job.depends_on == []

        # Explicit empty string
        base_job_params["build_job_id"] = ""
        job2 = CRSRunJob(**base_job_params)
        assert job2.depends_on == []


class TestCRSRunJobSnapshotIntegration:
    """Test SnapshotManager integration in CRSRunJob."""

    @patch("crsbench.experiment.jobs.crs_run.OssCrsAdapter")
    @patch("crsbench.experiment.jobs.crs_run.SnapshotManager")
    @patch("crsbench.experiment.jobs.crs_run.MetaYamlAdapter")
    def test_snapshot_manager_stop_called_after_execution(
        self,
        mock_meta_adapter_cls: MagicMock,
        mock_snapshot_cls: MagicMock,
        mock_adapter_cls: MagicMock,
        base_job_params: dict,
        tmp_path: Path,
    ) -> None:
        """Verify SnapshotManager.stop() called after execution."""
        # Setup mocks
        mock_meta_adapter = MagicMock()
        mock_meta_adapter.get_harness.return_value = MagicMock(name="test_harness")
        mock_meta_adapter.get_harness.return_value.vulns = []
        mock_meta_adapter_cls.from_benchmark_path.return_value = mock_meta_adapter

        mock_snapshot_mgr = MagicMock()
        mock_snapshot_mgr.cycle = 5
        mock_snapshot_cls.return_value = mock_snapshot_mgr

        mock_crs_adapter = MagicMock()
        mock_crs_adapter.run.return_value = MagicMock(
            success=True,
            error=None,
            output="test output",
            execution_time=100.0,
            timed_out=False,
        )
        mock_adapter_cls.return_value = mock_crs_adapter

        # Create trial output dir
        base_job_params["trial_output_dir"].mkdir(parents=True, exist_ok=True)

        job = CRSRunJob(**base_job_params)
        from crsbench.benchmark_ci.jobs.base import JobContext

        context = JobContext()

        # Execute
        result = job.execute(context)

        # Verify SnapshotManager.stop() was called
        mock_snapshot_mgr.stop.assert_called_once()
        assert result.success is True
        assert result.details["snapshots_captured"] == 5


class TestCRSRunJobEarlyTermination:
    """Test early termination support via stop_event."""

    @patch("crsbench.experiment.jobs.crs_run.OssCrsAdapter")
    @patch("crsbench.experiment.jobs.crs_run.SnapshotManager")
    @patch("crsbench.experiment.jobs.crs_run.POVVerificationManager")
    @patch("crsbench.experiment.jobs.crs_run.MetaYamlAdapter")
    def test_stop_event_passed_to_run_crs(
        self,
        mock_meta_adapter_cls: MagicMock,
        mock_pov_mgr_cls: MagicMock,
        mock_snapshot_cls: MagicMock,
        mock_adapter_cls: MagicMock,
        base_job_params: dict,
    ) -> None:
        """Verify stop_event is passed to adapter.run()."""
        import threading

        # Setup mocks
        mock_meta_adapter = MagicMock()
        mock_harness = MagicMock()
        mock_harness.vulns = [MagicMock(vuln_keyword="cpv_0")]
        mock_meta_adapter.get_harness.return_value = mock_harness
        mock_meta_adapter_cls.from_benchmark_path.return_value = mock_meta_adapter

        mock_pov_mgr = MagicMock()
        mock_pov_mgr.found_cpvs = {"cpv_0"}
        mock_pov_mgr._early_stop_triggered = True
        mock_pov_mgr_cls.return_value = mock_pov_mgr

        mock_snapshot_mgr = MagicMock()
        mock_snapshot_mgr.cycle = 3
        mock_snapshot_cls.return_value = mock_snapshot_mgr

        mock_crs_adapter = MagicMock()
        mock_crs_adapter.run.return_value = MagicMock(
            success=True,
            error=None,
            output="",
            execution_time=60.0,
            timed_out=False,
        )
        mock_adapter_cls.return_value = mock_crs_adapter

        # Create trial output dir
        base_job_params["trial_output_dir"].mkdir(parents=True, exist_ok=True)

        job = CRSRunJob(**base_job_params)
        from crsbench.benchmark_ci.jobs.base import JobContext

        context = JobContext()

        # Execute
        result = job.execute(context)

        # Verify adapter.run() was called with stop_event
        call_kwargs = mock_crs_adapter.run.call_args.kwargs
        assert "stop_event" in call_kwargs
        assert isinstance(call_kwargs["stop_event"], threading.Event)

        # Verify early termination is reported
        assert result.details["early_termination"] is True
        assert result.details["cpvs_found"] == ["cpv_0"]


class TestCRSRunJobBugFixingMode:
    """Test bug-fixing CRS mode (skip internal verification)."""

    @patch("crsbench.experiment.jobs.crs_run.OssCrsAdapter")
    @patch("crsbench.experiment.jobs.crs_run.SnapshotManager")
    @patch("crsbench.experiment.jobs.crs_run.MetaYamlAdapter")
    def test_bug_fixing_skips_pov_verification(
        self,
        mock_meta_adapter_cls: MagicMock,
        mock_snapshot_cls: MagicMock,
        mock_adapter_cls: MagicMock,
        base_job_params: dict,
    ) -> None:
        """Bug-fixing CRS should skip POVVerificationManager setup."""
        # Setup mocks
        mock_meta_adapter = MagicMock()
        mock_harness = MagicMock()
        mock_harness.vulns = [MagicMock(vuln_keyword="cpv_0")]
        mock_meta_adapter.get_harness.return_value = mock_harness
        mock_meta_adapter_cls.from_benchmark_path.return_value = mock_meta_adapter

        mock_snapshot_mgr = MagicMock()
        mock_snapshot_mgr.cycle = 2
        mock_snapshot_cls.return_value = mock_snapshot_mgr

        mock_crs_adapter = MagicMock()
        mock_crs_adapter.run.return_value = MagicMock(
            success=True,
            error=None,
            output="",
            execution_time=120.0,
            timed_out=False,
        )
        mock_adapter_cls.return_value = mock_crs_adapter

        # Create trial output dir
        base_job_params["trial_output_dir"].mkdir(parents=True, exist_ok=True)
        base_job_params["crs_type"] = "bug-fixing"

        job = CRSRunJob(**base_job_params)
        from crsbench.benchmark_ci.jobs.base import JobContext

        context = JobContext()

        # Execute
        with patch(
            "crsbench.experiment.jobs.crs_run.POVVerificationManager"
        ) as mock_pov_mgr_cls:
            result = job.execute(context)
            # POVVerificationManager should not be instantiated for bug-fixing
            mock_pov_mgr_cls.assert_not_called()

        assert result.success is True
        assert result.details["crs_type"] == "bug-fixing"


class TestCRSRunJobContextShared:
    """Test context.shared integration for build results."""

    @patch("crsbench.experiment.jobs.crs_run.OssCrsAdapter")
    @patch("crsbench.experiment.jobs.crs_run.SnapshotManager")
    def test_adapter_from_context_shared(
        self,
        mock_snapshot_cls: MagicMock,
        mock_adapter_cls: MagicMock,
        base_job_params: dict,
    ) -> None:
        """Verify adapter is retrieved from context.shared when build_job_id set."""
        # Setup mocks
        mock_meta_adapter = MagicMock()
        mock_harness = MagicMock()
        mock_harness.vulns = []
        mock_meta_adapter.get_harness.return_value = mock_harness

        mock_snapshot_mgr = MagicMock()
        mock_snapshot_mgr.cycle = 1
        mock_snapshot_cls.return_value = mock_snapshot_mgr

        mock_crs_adapter = MagicMock()
        mock_crs_adapter.run.return_value = MagicMock(
            success=True,
            error=None,
            output="",
            execution_time=50.0,
            timed_out=False,
        )
        mock_adapter_cls.return_value = mock_crs_adapter

        # Create trial output dir
        base_job_params["trial_output_dir"].mkdir(parents=True, exist_ok=True)
        base_job_params["build_job_id"] = "build:test-project"

        job = CRSRunJob(**base_job_params)
        from crsbench.benchmark_ci.jobs.base import JobContext

        context = JobContext(
            shared={
                "build:test-project": {
                    "adapter": mock_meta_adapter,
                    "build_results": {},
                }
            }
        )

        # Execute - should use MetaYamlAdapter from context.shared
        with patch(
            "crsbench.experiment.jobs.crs_run.MetaYamlAdapter"
        ) as mock_meta_adapter_cls:
            result = job.execute(context)
            # from_benchmark_path should NOT be called since adapter is in shared
            mock_meta_adapter_cls.from_benchmark_path.assert_not_called()

        assert result.success is True

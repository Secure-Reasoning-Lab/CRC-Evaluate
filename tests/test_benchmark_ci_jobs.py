"""Tests for benchmark_ci jobs module.

Tests the job-based approach to benchmark CI:
- Job base classes
- BuildJob, VerifyPovJob, VerifyPatchJob
- JobFactory
- ProjectCIRunner
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.benchmark_ci.factory import JobFactory, _parse_cpv_num
from crsbench.benchmark_ci.jobs import (
    BuildJob,
    JobResult,
    VerifyPatchJob,
    VerifyPovJob,
)
from crsbench.benchmark_ci.runner import ProjectCIResult
from crsbench.builder.types import BenchmarkMode, BuildConfig, VariantType


class TestJobResult:
    """Tests for JobResult dataclass."""

    def test_job_result_creation(self) -> None:
        """Test basic JobResult creation."""
        now = datetime.now()
        result = JobResult(
            job_id="build:test-bench-address-deltabase",
            job_type="build",
            success=True,
            started_at=now,
            finished_at=now,
            elapsed_seconds=10.5,
        )
        assert result.job_id == "build:test-bench-address-deltabase"
        assert result.job_type == "build"
        assert result.success is True
        assert result.elapsed_seconds == 10.5

    def test_job_result_with_error(self) -> None:
        """Test JobResult with error."""
        now = datetime.now()
        result = JobResult(
            job_id="verify-pov:test-address-deltaref:pov_0",
            job_type="verify-pov",
            success=False,
            started_at=now,
            finished_at=now,
            elapsed_seconds=2.0,
            error="POV did not crash",
        )
        assert result.success is False
        assert result.error == "POV did not crash"

    def test_job_result_to_dict(self) -> None:
        """Test JobResult.to_dict() serialization."""
        now = datetime.now()
        result = JobResult(
            job_id="build:test-address-deltabase",
            job_type="build",
            success=True,
            started_at=now,
            finished_at=now,
            elapsed_seconds=5.0,
            details={"variant_name": "test-address-deltabase", "cached": False},
        )
        d = result.to_dict()
        assert d["job_id"] == "build:test-address-deltabase"
        assert d["job_type"] == "build"
        assert d["success"] is True
        assert d["elapsed_seconds"] == 5.0
        assert d["details"]["variant_name"] == "test-address-deltabase"


class TestBuildJob:
    """Tests for BuildJob."""

    def test_build_job_properties(self) -> None:
        """Test BuildJob property methods."""
        config = BuildConfig(
            benchmark_name="test-bench",
            variant_type=VariantType.DELTA_BASE,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path=Path("/tmp/test"),
            mode=BenchmarkMode.DELTA,
            sanitizer="address",
        )
        job = BuildJob(
            benchmark="test-bench",
            sanitizer="address",
            variant_type="deltabase",
            config=config,
        )
        assert job.job_id == "build:test-bench-address-deltabase"
        assert job.job_type == "build"
        assert job.depends_on == []
        assert job.variant_name == "test-bench-address-deltabase"


class TestVerifyPovJob:
    """Tests for VerifyPovJob."""

    def test_verify_pov_job_properties(self) -> None:
        """Test VerifyPovJob property methods."""
        job = VerifyPovJob(
            benchmark="test-bench",
            sanitizer="address",
            variant_type="deltaref",
            pov_id="pov_0",
            pov_path=Path("/tmp/pov_0.blob"),
            harness="harness_a",
            expected_crash=True,
        )
        assert job.job_id == "verify-pov:test-bench-address-deltaref:pov_0"
        assert job.job_type == "verify-pov"
        assert job.depends_on == ["build:test-bench-address-deltaref"]
        assert job.variant_name == "test-bench-address-deltaref"

    def test_verify_pov_job_cpv_variant(self) -> None:
        """Test VerifyPovJob with CPV variant."""
        job = VerifyPovJob(
            benchmark="test-bench",
            sanitizer="address",
            variant_type="cpv0",
            pov_id="pov_0",
            pov_path=Path("/tmp/pov_0.blob"),
            harness="harness_a",
            expected_crash=True,
        )
        assert job.job_id == "verify-pov:test-bench-address-cpv0:pov_0"
        assert job.depends_on == ["build:test-bench-address-cpv0"]

    def test_get_error_message_expected_crash_no_crash(self) -> None:
        """Test error message when expected crash but didn't."""
        job = VerifyPovJob(
            benchmark="test-bench",
            sanitizer="address",
            variant_type="deltaref",
            pov_id="pov_0",
            pov_path=Path("/tmp/pov_0.blob"),
            harness="harness_a",
            expected_crash=True,
        )
        msg = job._get_error_message(actual_crash=False)
        assert "did NOT crash" in msg
        assert "expected crash" in msg

    def test_get_error_message_zeroday(self) -> None:
        """Test error message for ZERODAY (crash on deltabase)."""
        job = VerifyPovJob(
            benchmark="test-bench",
            sanitizer="address",
            variant_type="deltabase",
            pov_id="pov_0",
            pov_path=Path("/tmp/pov_0.blob"),
            harness="harness_a",
            expected_crash=False,
        )
        msg = job._get_error_message(actual_crash=True)
        assert "ZERODAY" in msg

    def test_get_error_message_unintended(self) -> None:
        """Test error message for unintended crash on allpatched."""
        job = VerifyPovJob(
            benchmark="test-bench",
            sanitizer="address",
            variant_type="allpatched",
            pov_id="pov_0",
            pov_path=Path("/tmp/pov_0.blob"),
            harness="harness_a",
            expected_crash=False,
        )
        msg = job._get_error_message(actual_crash=True)
        assert "UNINTENDED" in msg


class TestVerifyPatchJob:
    """Tests for VerifyPatchJob."""

    def test_verify_patch_job_properties(self) -> None:
        """Test VerifyPatchJob property methods."""
        job = VerifyPatchJob(
            benchmark="test-bench",
            sanitizer="address",
            cpv_num=0,
            patch_path=Path("/tmp/patch_0.diff"),
            povs_for_cpv=[("pov_0", Path("/tmp/pov_0.blob"))],
            harness="harness_a",
        )
        assert job.job_id == "verify-patch:test-bench-address-cpv0"
        assert job.job_type == "verify-patch"
        assert job.depends_on == ["build:test-bench-address-patched-cpv0"]
        assert job.variant_name == "test-bench-address-patched-cpv0"


class TestParseCpvNum:
    """Tests for _parse_cpv_num helper."""

    def test_parse_cpv_num_valid(self) -> None:
        """Test parsing valid CPV keywords."""
        assert _parse_cpv_num("cpv_0") == 0
        assert _parse_cpv_num("cpv_1") == 1
        assert _parse_cpv_num("cpv_10") == 10

    def test_parse_cpv_num_invalid(self) -> None:
        """Test parsing invalid CPV keywords."""
        with pytest.raises(ValueError):
            _parse_cpv_num("invalid")
        with pytest.raises(ValueError):
            _parse_cpv_num("cvp_0")


class TestJobFactory:
    """Tests for JobFactory."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock MetaYamlAdapter."""
        adapter = MagicMock()
        adapter.benchmark_name = "test-bench"
        adapter.get_mode.return_value = BenchmarkMode.DELTA
        adapter.get_base_commit.return_value = "base123"
        adapter.get_ref_commit.return_value = "ref456"
        adapter.main_repo = "https://github.com/test/repo"
        adapter.benchmark_path = Path("/tmp/test-bench")
        adapter.lang = "c"
        adapter.repo_name = None
        adapter.get_cpv_numbers.return_value = [0, 1]
        adapter.get_harness_names.return_value = ["harness_a"]
        return adapter

    def test_factory_create_all_jobs_delta_mode(self, mock_adapter: MagicMock) -> None:
        """Test job creation for DELTA mode benchmark."""
        # Setup mock POV data
        mock_pov = MagicMock()
        mock_pov.id = "pov_0"
        mock_adapter.get_all_povs.return_value = [("cpv_0", mock_pov)]
        mock_adapter.get_pov_path.return_value = Path("/tmp/pov_0.blob")
        mock_adapter.get_patch_path.return_value = Path("/tmp/patch_0.diff")

        # Mock path.exists() to return True
        with patch.object(Path, "exists", return_value=True):
            factory = JobFactory(mock_adapter, sanitizer="address")
            jobs = factory.create_all_jobs()

        # Should have build jobs for: deltabase, deltaref, allpatched, cpv0, cpv1, patched-cpv0, patched-cpv1
        build_jobs = [j for j in jobs if j.job_type == "build"]
        assert len(build_jobs) == 7

        # Check variant types
        variant_types = [j.variant_type for j in build_jobs]
        assert "deltabase" in variant_types
        assert "deltaref" in variant_types
        assert "allpatched" in variant_types
        assert "cpv0" in variant_types
        assert "cpv1" in variant_types
        assert "patched-cpv0" in variant_types
        assert "patched-cpv1" in variant_types

    def test_factory_create_verify_pov_jobs(self, mock_adapter: MagicMock) -> None:
        """Test VerifyPovJob creation with correct expected_crash values."""
        mock_pov = MagicMock()
        mock_pov.id = "pov_0"
        mock_adapter.get_all_povs.return_value = [("cpv_0", mock_pov)]
        mock_adapter.get_pov_path.return_value = Path("/tmp/pov_0.blob")
        mock_adapter.get_patch_path.return_value = Path("/tmp/patch_0.diff")

        with patch.object(Path, "exists", return_value=True):
            factory = JobFactory(mock_adapter, sanitizer="address")
            jobs = factory._create_verify_pov_jobs()

        # Should have verify jobs for: deltabase, deltaref, allpatched, cpv0, cpv1
        assert len(jobs) == 5

        # Check expected_crash values
        job_map = {j.variant_type: j for j in jobs}

        # deltabase: should NOT crash
        assert job_map["deltabase"].expected_crash is False

        # deltaref: should crash
        assert job_map["deltaref"].expected_crash is True

        # allpatched: should NOT crash
        assert job_map["allpatched"].expected_crash is False

        # cpv0: should crash (pov_0 targets cpv_0)
        assert job_map["cpv0"].expected_crash is True

        # cpv1: should NOT crash (pov_0 doesn't target cpv_1)
        assert job_map["cpv1"].expected_crash is False


class TestProjectCIResult:
    """Tests for ProjectCIResult."""

    def test_result_properties(self) -> None:
        """Test ProjectCIResult property methods."""
        now = datetime.now()
        results = [
            JobResult(
                job_id="build:test-address-deltabase",
                job_type="build",
                success=True,
                started_at=now,
                finished_at=now,
                elapsed_seconds=10.0,
            ),
            JobResult(
                job_id="verify-pov:test-address-deltaref:pov_0",
                job_type="verify-pov",
                success=True,
                started_at=now,
                finished_at=now,
                elapsed_seconds=2.0,
            ),
            JobResult(
                job_id="verify-pov:test-address-deltabase:pov_0",
                job_type="verify-pov",
                success=False,
                started_at=now,
                finished_at=now,
                elapsed_seconds=2.0,
                error="Unexpected crash",
            ),
        ]

        result = ProjectCIResult(
            started_at=now,
            finished_at=now,
            results=results,
        )

        assert len(result.build_results) == 1
        assert len(result.verify_results) == 2
        assert result.total_build_time == 10.0
        assert result.total_verify_time == 4.0
        assert result.passed is False
        assert result.passed_count == 2
        assert result.failed_count == 1

    def test_get_summary(self) -> None:
        """Test ProjectCIResult.get_summary()."""
        now = datetime.now()
        results = [
            JobResult(
                job_id="build:test",
                job_type="build",
                success=True,
                started_at=now,
                finished_at=now,
                elapsed_seconds=5.0,
            ),
        ]
        result = ProjectCIResult(
            started_at=now,
            finished_at=now,
            results=results,
        )
        summary = result.get_summary()
        assert summary["total_jobs"] == 1
        assert summary["passed"] == 1
        assert summary["failed"] == 0
        assert summary["build_time_seconds"] == 5.0

    def test_get_failed_jobs(self) -> None:
        """Test ProjectCIResult.get_failed_jobs()."""
        now = datetime.now()
        results = [
            JobResult(
                job_id="build:test-1",
                job_type="build",
                success=True,
                started_at=now,
                finished_at=now,
                elapsed_seconds=1.0,
            ),
            JobResult(
                job_id="build:test-2",
                job_type="build",
                success=False,
                started_at=now,
                finished_at=now,
                elapsed_seconds=1.0,
                error="Build failed",
            ),
        ]
        result = ProjectCIResult(
            started_at=now,
            finished_at=now,
            results=results,
        )
        failed = result.get_failed_jobs()
        assert len(failed) == 1
        assert failed[0].job_id == "build:test-2"

    def test_to_csv_rows(self) -> None:
        """Test ProjectCIResult.to_csv_rows()."""
        now = datetime.now()
        results = [
            JobResult(
                job_id="build:test",
                job_type="build",
                success=True,
                started_at=now,
                finished_at=now,
                elapsed_seconds=5.0,
                details={"variant_name": "test-deltabase"},
            ),
        ]
        result = ProjectCIResult(
            started_at=now,
            finished_at=now,
            results=results,
        )
        rows = result.to_csv_rows()
        assert len(rows) == 1
        assert rows[0]["job_id"] == "build:test"
        assert rows[0]["success"] is True
        assert rows[0]["detail_variant_name"] == "test-deltabase"

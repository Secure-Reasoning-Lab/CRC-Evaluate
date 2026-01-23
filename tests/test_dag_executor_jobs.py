"""Tests for new DAG job primitives.

Tests cover:
- BuildPatchJob: properties, dependency edges, execute behavior
- PatchTestJob: properties, dependency edges, execute with POV results
- CollectCoverageJob: properties, dependency edges, placeholder behavior
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from crsbench.benchmark_ci.jobs import BuildPatchJob, CollectCoverageJob
from crsbench.benchmark_ci.jobs.base import JobContext
from crsbench.benchmark_ci.jobs.patch import TestPatchJob as PatchTestJob


@pytest.fixture
def mock_context() -> JobContext:
    ctx = MagicMock()
    ctx.timeout = 120
    return ctx


# --- TestBuildPatchJob ---


class TestBuildPatchJob:
    def test_job_properties(self) -> None:
        job = BuildPatchJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=0,
            patch_path=Path("/patches/patch_0.diff"),
            config=MagicMock(),
        )

        assert job.job_id == "build-patch:curl-address-cpv0"
        assert job.job_type == "build-patch"
        assert job.variant_name == "curl-address-patched-cpv0"

    def test_depends_on(self) -> None:
        job = BuildPatchJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=2,
            patch_path=Path("/patches/patch_2.diff"),
            config=MagicMock(),
        )

        assert job.depends_on == ["build:curl-address-deltaref"]

    def test_execute_success(self, mock_context: JobContext) -> None:
        mock_build_result = MagicMock()
        mock_build_result.success = True
        mock_build_result.elapsed_seconds = 5.0
        mock_build_result.error = None
        mock_build_result.build_path = Path("/out/patched")
        mock_build_result.variant_name = "curl-address-patched-cpv0"
        mock_build_result.cached = False

        mock_context.builder.build_single.return_value = mock_build_result

        job = BuildPatchJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=0,
            patch_path=Path("/patches/patch_0.diff"),
            config=MagicMock(),
        )

        result = job.execute(mock_context)

        assert result.success is True
        assert result.job_id == "build-patch:curl-address-cpv0"
        assert result.artifacts["build_path"] == Path("/out/patched")
        assert result.details["cpv_num"] == 0

    def test_execute_failure(self, mock_context: JobContext) -> None:
        mock_context.builder.build_single.side_effect = RuntimeError("build crashed")

        job = BuildPatchJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=0,
            patch_path=Path("/patches/patch_0.diff"),
            config=MagicMock(),
        )

        result = job.execute(mock_context)

        assert result.success is False
        assert "build crashed" in result.error


# --- TestPatchTestJob ---


class TestPatchTestJob:
    def test_job_properties(self) -> None:
        job = PatchTestJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=1,
            povs_for_cpv=[("pov_0", Path("/povs/pov_0"))],
            harness="harness_0",
        )

        assert job.job_id == "test-patch:curl-address-cpv1"
        assert job.job_type == "test-patch"
        assert job.variant_name == "curl-address-patched-cpv1"

    def test_depends_on(self) -> None:
        job = PatchTestJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=3,
        )

        assert job.depends_on == ["build-patch:curl-address-cpv3"]

    def test_execute_all_pass(self, mock_context: JobContext, tmp_path: Path) -> None:
        """All POVs don't crash -> success."""
        pov_file = tmp_path / "pov_0"
        pov_file.write_bytes(b"pov data")

        mock_output = MagicMock()
        mock_output.crashed = False
        mock_context.infra.reproduce.return_value = mock_output

        job = PatchTestJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=0,
            povs_for_cpv=[("pov_0", pov_file)],
            harness="harness_0",
        )

        result = job.execute(mock_context)

        assert result.success is True
        assert result.details["fixed"] == 1
        assert result.details["failed"] == []

    def test_execute_some_fail(self, mock_context: JobContext, tmp_path: Path) -> None:
        """One POV crashes -> failure."""
        pov0 = tmp_path / "pov_0"
        pov0.write_bytes(b"data0")
        pov1 = tmp_path / "pov_1"
        pov1.write_bytes(b"data1")

        mock_pass = MagicMock()
        mock_pass.crashed = False
        mock_crash = MagicMock()
        mock_crash.crashed = True

        mock_context.infra.reproduce.side_effect = [mock_pass, mock_crash]

        job = PatchTestJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=0,
            povs_for_cpv=[("pov_0", pov0), ("pov_1", pov1)],
            harness="harness_0",
        )

        result = job.execute(mock_context)

        assert result.success is False
        assert "pov_1" in result.details["failed"]
        assert "pov_1" in result.error

    def test_execute_exception(self, mock_context: JobContext, tmp_path: Path) -> None:
        """reproduce() raises -> failure with error."""
        pov_file = tmp_path / "pov_0"
        pov_file.write_bytes(b"data")

        mock_context.infra.reproduce.side_effect = RuntimeError("timeout")

        job = PatchTestJob(
            benchmark="curl",
            sanitizer="address",
            cpv_num=0,
            povs_for_cpv=[("pov_0", pov_file)],
            harness="harness_0",
        )

        result = job.execute(mock_context)

        assert result.success is False
        assert "timeout" in result.error


# --- TestCollectCoverageJob ---


class TestCollectCoverageJob:
    def test_job_properties(self) -> None:
        job = CollectCoverageJob(
            benchmark="curl",
            sanitizer="address",
            variant_type="coverage",
            harness="harness_0",
        )

        assert job.job_id == "collect-coverage:curl-address-coverage"
        assert job.job_type == "collect-coverage"
        assert job.variant_name == "curl-address-coverage"

    def test_depends_on(self) -> None:
        job = CollectCoverageJob(
            benchmark="curl",
            sanitizer="address",
            variant_type="coverage",
            harness="harness_0",
        )

        assert job.depends_on == ["build:curl-address-coverage"]

    def test_execute_no_coverage_method(self, mock_context: JobContext) -> None:
        """If infra has no coverage method, fails with NotImplementedError."""
        # Remove coverage attribute
        del mock_context.infra.coverage

        job = CollectCoverageJob(
            benchmark="curl",
            sanitizer="address",
            variant_type="coverage",
            harness="harness_0",
        )

        result = job.execute(mock_context)

        assert result.success is False
        assert "not yet wired" in result.error.lower()

    def test_execute_success(self, mock_context: JobContext) -> None:
        """Coverage collection succeeds."""
        mock_context.infra.coverage.return_value = None

        job = CollectCoverageJob(
            benchmark="curl",
            sanitizer="address",
            variant_type="coverage",
            harness="harness_0",
            corpus_dir=Path("/corpus"),
        )

        result = job.execute(mock_context)

        assert result.success is True
        assert result.details["harness"] == "harness_0"
        mock_context.infra.coverage.assert_called_once_with(
            project_name="curl-address-coverage",
            harness="harness_0",
            corpus_dir=Path("/corpus"),
            timeout=120,
        )

    def test_execute_failure(self, mock_context: JobContext) -> None:
        """Coverage collection raises -> failure."""
        mock_context.infra.coverage.side_effect = RuntimeError("disk full")

        job = CollectCoverageJob(
            benchmark="curl",
            sanitizer="address",
            variant_type="coverage",
            harness="harness_0",
        )

        result = job.execute(mock_context)

        assert result.success is False
        assert "disk full" in result.error

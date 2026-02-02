"""Unit tests for crsbench.experiment.post_trial.

Tests verify:
- Job creation from trial results
- Failed trials are skipped
- Coverage jobs created when enabled
- Patch build/test jobs created for bug-fixing CRS
- Dependencies set correctly
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
)
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.experiment.post_trial import (
    TrialResult,
    create_post_trial_jobs,
    execute_post_trial_analysis,
)


class TestTrialResult:
    """Tests for TrialResult dataclass."""

    def test_trial_result_creation(self, tmp_path: Path) -> None:
        """TrialResult can be created with required fields."""
        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
        )
        assert result.trial_id == "trial-001"
        assert result.success is True
        assert result.cpvs_found == []
        assert result.patches == []

    def test_trial_result_with_cpvs(self, tmp_path: Path) -> None:
        """TrialResult with cpvs_found list."""
        result = TrialResult(
            trial_id="trial-002",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
            cpvs_found=["cpv_0", "cpv_1"],
        )
        assert result.cpvs_found == ["cpv_0", "cpv_1"]

    def test_trial_result_with_patches(self, tmp_path: Path) -> None:
        """TrialResult with patches list."""
        patches = [tmp_path / "patch1.diff", tmp_path / "patch2.diff"]
        result = TrialResult(
            trial_id="trial-003",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            patches=patches,
        )
        assert len(result.patches) == 2


class TestCreatePostTrialJobsSkipsFailed:
    """Tests for skipping failed trials."""

    def test_skips_failed_trial(self, tmp_path: Path) -> None:
        """Failed trials do not create any jobs."""
        result = TrialResult(
            trial_id="trial-failed",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=False,  # Failed trial
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([result], {})

        assert len(jobs) == 0

    def test_skips_only_failed_trials(self, tmp_path: Path) -> None:
        """Only failed trials are skipped, successful ones create jobs."""
        failed_result = TrialResult(
            trial_id="trial-failed",
            benchmark_path=tmp_path / "benchmark1",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output1",
            success=False,
            crs_type="bug_finding",
        )
        success_result = TrialResult(
            trial_id="trial-success",
            benchmark_path=tmp_path / "benchmark2",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output2",
            success=True,
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([failed_result, success_result], {})

        # Only successful trial creates jobs
        assert len(jobs) == 1
        assert isinstance(jobs[0], FlatCollectCoverageJob)


class TestCreatePostTrialJobsCoverage:
    """Tests for coverage job creation."""

    def test_creates_coverage_job_when_enabled(self, tmp_path: Path) -> None:
        """Coverage job created for successful trial when coverage_enabled=True."""
        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=True)

        assert len(jobs) == 1
        assert isinstance(jobs[0], FlatCollectCoverageJob)
        assert jobs[0].benchmark_name == "benchmark"
        assert jobs[0].harness == "test_harness"

    def test_no_coverage_job_when_disabled(self, tmp_path: Path) -> None:
        """No coverage job created when coverage_enabled=False."""
        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=False)

        assert len(jobs) == 0

    def test_coverage_job_has_correct_build_job_id(self, tmp_path: Path) -> None:
        """Coverage job has correct build_job_id dependency."""
        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
        )
        build_job_ids = {"benchmark": "build-variants:benchmark"}

        jobs = create_post_trial_jobs([result], build_job_ids, coverage_enabled=True)

        assert len(jobs) == 1
        coverage_job = jobs[0]
        assert isinstance(coverage_job, FlatCollectCoverageJob)
        assert coverage_job.build_job_id == "build-variants:benchmark"


class TestCreatePostTrialJobsPatches:
    """Tests for patch job creation."""

    def test_creates_patch_jobs_for_bug_fixing(self, tmp_path: Path) -> None:
        """Patch build + test jobs created for bug-fixing CRS with patches."""
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("--- a/file\n+++ b/file\n")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            cpvs_found=["cpv_0"],
            patches=[patch_path],
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=False)

        # Should have build + test job for the patch
        assert len(jobs) == 2
        build_job = jobs[0]
        test_job = jobs[1]

        assert isinstance(build_job, BuildPatchVariantJob)
        assert isinstance(test_job, PatchVariantTestJob)
        assert build_job.patch_path == patch_path
        assert test_job.patch_id == "cpv_0_patch"

    def test_patch_test_depends_on_patch_build(self, tmp_path: Path) -> None:
        """PatchVariantTestJob depends on BuildPatchVariantJob."""
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("--- a/file\n+++ b/file\n")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            patches=[patch_path],
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=False)

        build_job = jobs[0]
        test_job = jobs[1]

        assert isinstance(build_job, BuildPatchVariantJob)
        assert isinstance(test_job, PatchVariantTestJob)
        assert test_job.build_patch_job_id == build_job.job_id

    def test_no_patch_jobs_for_bug_finding(self, tmp_path: Path) -> None:
        """Bug-finding CRS does not create patch jobs even with patches."""
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("--- a/file\n+++ b/file\n")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",  # Not bug_fixing
            patches=[patch_path],
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=False)

        # No jobs for bug-finding CRS when coverage disabled
        assert len(jobs) == 0

    def test_multiple_patches_create_multiple_jobs(self, tmp_path: Path) -> None:
        """Multiple patches create multiple build+test pairs."""
        patch1 = tmp_path / "cpv_0_patch1.diff"
        patch2 = tmp_path / "cpv_0_patch2.diff"
        patch1.write_text("patch1")
        patch2.write_text("patch2")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            patches=[patch1, patch2],
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=False)

        # 2 patches = 4 jobs (2 build + 2 test)
        assert len(jobs) == 4
        build_jobs = [j for j in jobs if isinstance(j, BuildPatchVariantJob)]
        test_jobs = [j for j in jobs if isinstance(j, PatchVariantTestJob)]
        assert len(build_jobs) == 2
        assert len(test_jobs) == 2


class TestCreatePostTrialJobsDependencies:
    """Tests for job dependency setup."""

    def test_coverage_job_depends_on_build(self, tmp_path: Path) -> None:
        """Coverage job has correct depends_on."""
        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
        )
        build_job_ids = {"benchmark": "build-variants:benchmark"}

        jobs = create_post_trial_jobs([result], build_job_ids)

        coverage_job = jobs[0]
        assert coverage_job.depends_on == ["build-variants:benchmark"]

    def test_patch_build_depends_on_main_build(self, tmp_path: Path) -> None:
        """BuildPatchVariantJob depends on main build job."""
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("patch")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            patches=[patch_path],
        )
        build_job_ids = {"benchmark": "build-variants:benchmark"}

        jobs = create_post_trial_jobs([result], build_job_ids, coverage_enabled=False)

        build_job = jobs[0]
        assert isinstance(build_job, BuildPatchVariantJob)
        assert build_job.depends_on == ["build-variants:benchmark"]

    def test_empty_build_job_ids_uses_empty_string(self, tmp_path: Path) -> None:
        """When build_job_ids is empty, jobs have empty build_job_id."""
        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([result], {})

        assert len(jobs) == 1
        coverage_job = jobs[0]
        assert isinstance(coverage_job, FlatCollectCoverageJob)
        assert coverage_job.build_job_id == ""
        assert coverage_job.depends_on == []


class TestCreatePostTrialJobsEmpty:
    """Tests for empty inputs."""

    def test_empty_trial_results(self) -> None:
        """Empty trial results returns empty job list."""
        jobs = create_post_trial_jobs([], {})
        assert jobs == []

    def test_all_failed_trials(self, tmp_path: Path) -> None:
        """All failed trials returns empty job list."""
        results = [
            TrialResult(
                trial_id=f"trial-{i}",
                benchmark_path=tmp_path / f"benchmark{i}",
                harness_name="test_harness",
                trial_output_dir=tmp_path / f"output{i}",
                success=False,
                crs_type="bug_finding",
            )
            for i in range(3)
        ]

        jobs = create_post_trial_jobs(results, {})

        assert jobs == []


class TestExecutePostTrialAnalysis:
    """Tests for execute_post_trial_analysis function."""

    def test_empty_jobs_returns_empty_results(self) -> None:
        """Empty job list returns empty results dict."""
        results = execute_post_trial_analysis([], redis_host="localhost")
        assert results == {}

    @patch("crsbench.distributed.ci_jobs.ci_results_to_executor_results")
    @patch("crsbench.distributed.ci_jobs.enqueue_and_poll_ci_jobs")
    def test_uses_redis_for_job_execution(
        self,
        mock_enqueue: MagicMock,
        mock_convert: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Jobs are executed via Redis using enqueue_and_poll_ci_jobs."""
        job = FlatCollectCoverageJob(
            benchmark_path=tmp_path / "benchmark",
            benchmark_name="benchmark",
            harness="test_harness",
            build_job_id="",
        )

        # Mock raw results from Redis
        mock_raw_results = {"raw": "results"}
        mock_enqueue.return_value = mock_raw_results

        # Mock converted ExecutorResult dict
        mock_executor_results = {
            job.job_id: ExecutorResult(
                job_id=job.job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            )
        }
        mock_convert.return_value = mock_executor_results

        results = execute_post_trial_analysis(
            [job], redis_host="test-redis", queue_name="test-queue"
        )

        # Verify enqueue_and_poll_ci_jobs was called correctly
        mock_enqueue.assert_called_once_with(
            [job], "test-redis", queue_name="test-queue"
        )

        # Verify conversion was called with raw results
        mock_convert.assert_called_once_with(mock_raw_results)

        # Verify final results
        assert results == mock_executor_results

    @patch("crsbench.distributed.ci_jobs.ci_results_to_executor_results")
    @patch("crsbench.distributed.ci_jobs.enqueue_and_poll_ci_jobs")
    def test_results_track_success_failure(
        self,
        mock_enqueue: MagicMock,
        mock_convert: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Results correctly track success and failure."""
        job1 = FlatCollectCoverageJob(
            benchmark_path=tmp_path / "benchmark1",
            benchmark_name="benchmark1",
            harness="harness1",
            build_job_id="",
        )
        job2 = FlatCollectCoverageJob(
            benchmark_path=tmp_path / "benchmark2",
            benchmark_name="benchmark2",
            harness="harness2",
            build_job_id="",
        )

        mock_results = {
            job1.job_id: ExecutorResult(
                job_id=job1.job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            job2.job_id: ExecutorResult(
                job_id=job2.job_id,
                status=JobStatus.FAILED,
                elapsed_seconds=2.0,
                error="Coverage collection failed",
            ),
        }

        mock_enqueue.return_value = {"raw": "results"}
        mock_convert.return_value = mock_results

        results = execute_post_trial_analysis([job1, job2], redis_host="localhost")

        assert results[job1.job_id].success
        assert not results[job2.job_id].success

    @patch("crsbench.distributed.ci_jobs.ci_results_to_executor_results")
    @patch("crsbench.distributed.ci_jobs.enqueue_and_poll_ci_jobs")
    def test_default_redis_host_and_queue_name(
        self,
        mock_enqueue: MagicMock,
        mock_convert: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Default redis_host and queue_name are used when not specified."""
        job = FlatCollectCoverageJob(
            benchmark_path=tmp_path / "benchmark",
            benchmark_name="benchmark",
            harness="test_harness",
            build_job_id="",
        )

        mock_enqueue.return_value = {}
        mock_convert.return_value = {}

        execute_post_trial_analysis([job])

        # Verify default values
        mock_enqueue.assert_called_once_with(
            [job], "localhost", queue_name="crsbench_post_trial_verify"
        )


class TestCreatePostTrialJobsSourceMode:
    """Tests for source_mode parameter."""

    def test_source_mode_default_is_pkgs(self, tmp_path: Path) -> None:
        """Default source_mode is 'pkgs'."""
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("patch")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            patches=[patch_path],
        )

        jobs = create_post_trial_jobs([result], {}, coverage_enabled=False)

        build_job = jobs[0]
        assert isinstance(build_job, BuildPatchVariantJob)
        assert build_job.source_mode == "pkgs"

    def test_source_mode_pkgs_passed_to_patch_jobs(self, tmp_path: Path) -> None:
        """source_mode='pkgs' is passed to BuildPatchVariantJob."""
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("patch")

        result = TrialResult(
            trial_id="trial-001",
            benchmark_path=tmp_path / "benchmark",
            harness_name="test_harness",
            trial_output_dir=tmp_path / "output",
            success=True,
            crs_type="bug_fixing",
            patches=[patch_path],
        )

        jobs = create_post_trial_jobs(
            [result], {}, coverage_enabled=False, source_mode="pkgs"
        )

        build_job = jobs[0]
        assert isinstance(build_job, BuildPatchVariantJob)
        assert build_job.source_mode == "pkgs"

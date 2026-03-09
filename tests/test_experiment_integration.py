"""Integration tests for the experiment post-trial analysis flow.

Tests verify:
1. CRS trial execution via CRSRunJob (produces TrialResult)
2. Post-trial analysis via create_post_trial_jobs + execute_post_trial_analysis
   - Coverage collection for successful trials
   - Patch verification for bug-fixing CRS with patches
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
)
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.experiment import (
    CRSRunJob,
    TrialResult,
    create_post_trial_jobs,
    execute_post_trial_analysis,
)


class TestExperimentFlowMock:
    """Test post-trial analysis flow with mocked dependencies."""

    @patch("crsbench.distributed.ci_jobs.enqueue_and_poll_ci_jobs")
    @patch("crsbench.distributed.ci_jobs.ci_results_to_executor_results")
    def test_post_trial_analysis_coverage(
        self,
        mock_ci_results_converter: MagicMock,
        mock_enqueue_and_poll: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test post-trial analysis for bug-finding CRS with coverage collection."""
        # Setup benchmarks
        benchmark1_path = tmp_path / "benchmark1"
        benchmark2_path = tmp_path / "benchmark2"
        benchmark1_path.mkdir()
        benchmark2_path.mkdir()

        # --- Simulate CRS Trial Results ---
        trial_results = [
            TrialResult(
                trial_id="trial-1",
                benchmark_path=benchmark1_path,
                harness_name="fuzz_target",
                trial_output_dir=tmp_path / "trial1",
                success=True,
                crs_type="bug_finding",
                cpvs_found=["cpv_0"],
            ),
            TrialResult(
                trial_id="trial-2",
                benchmark_path=benchmark2_path,
                harness_name="fuzz_target",
                trial_output_dir=tmp_path / "trial2",
                success=False,  # Failed trial
                crs_type="bug_finding",
            ),
        ]

        # --- Post-Trial Analysis ---
        build_job_ids = {
            "benchmark1": "build-job-1",
            "benchmark2": "build-job-2",
        }

        post_trial_jobs = create_post_trial_jobs(
            trial_results,
            build_job_ids,
            coverage_enabled=True,
        )

        # Only 1 job (failed trial skipped)
        assert len(post_trial_jobs) == 1
        assert isinstance(post_trial_jobs[0], FlatCollectCoverageJob)
        assert post_trial_jobs[0].benchmark_name == "benchmark1"

        # Mock Redis execution
        mock_enqueue_and_poll.return_value = {
            post_trial_jobs[0].job_id: {
                "job_id": post_trial_jobs[0].job_id,
                "status": "SUCCESS",
            }
        }

        mock_ci_results_converter.return_value = {
            post_trial_jobs[0].job_id: ExecutorResult(
                job_id=post_trial_jobs[0].job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=10.0,
            )
        }

        post_trial_results = execute_post_trial_analysis(
            post_trial_jobs,
            redis_host="localhost",
        )

        assert len(post_trial_results) == 1
        assert post_trial_results[post_trial_jobs[0].job_id].success

        # Verify Redis calls
        mock_enqueue_and_poll.assert_called_once()
        mock_ci_results_converter.assert_called_once()

    @patch("crsbench.distributed.ci_jobs.enqueue_and_poll_ci_jobs")
    @patch("crsbench.distributed.ci_jobs.ci_results_to_executor_results")
    def test_post_trial_analysis_bug_fixing_crs(
        self,
        mock_ci_results_converter: MagicMock,
        mock_enqueue_and_poll: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test post-trial analysis for bug-fixing CRS with patches."""
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        # Create a patch file
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("--- a/file\n+++ b/file\n")

        # Create trial output structure
        trial_output_dir = tmp_path / "trial1"
        pov_dir = trial_output_dir / "output" / "povs"
        pov_dir.mkdir(parents=True)
        (pov_dir / "pov_0.bin").write_bytes(b"crash_input")

        # --- Bug-fixing trial result ---
        trial_results = [
            TrialResult(
                trial_id="trial-1",
                benchmark_path=benchmark_path,
                harness_name="fuzz_target",
                trial_output_dir=trial_output_dir,
                success=True,
                crs_type="bug_fixing",
                cpvs_found=["cpv_0"],
                patches=[patch_path],
            ),
        ]

        # --- Post-Trial Analysis ---
        build_job_ids = {"benchmark": "build-job-1"}

        post_trial_jobs = create_post_trial_jobs(
            trial_results,
            build_job_ids,
            coverage_enabled=True,
        )

        # Should have: 1 coverage + 1 patch build + 1 patch test = 3 jobs
        assert len(post_trial_jobs) == 3

        coverage_jobs = [
            j for j in post_trial_jobs if isinstance(j, FlatCollectCoverageJob)
        ]
        build_patch_jobs = [
            j for j in post_trial_jobs if isinstance(j, BuildPatchVariantJob)
        ]
        test_patch_jobs = [
            j for j in post_trial_jobs if isinstance(j, PatchVariantTestJob)
        ]

        assert len(coverage_jobs) == 1
        assert len(build_patch_jobs) == 1
        assert len(test_patch_jobs) == 1

        # Verify dependency chain
        test_job = test_patch_jobs[0]
        build_job = build_patch_jobs[0]
        assert test_job.build_patch_job_id == build_job.job_id

        # Mock Redis execution
        mock_raw_results = {
            j.job_id: {"job_id": j.job_id, "status": "SUCCESS"} for j in post_trial_jobs
        }
        mock_enqueue_and_poll.return_value = mock_raw_results

        mock_executor_results = {
            j.job_id: ExecutorResult(
                job_id=j.job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            )
            for j in post_trial_jobs
        }
        mock_ci_results_converter.return_value = mock_executor_results

        results = execute_post_trial_analysis(
            post_trial_jobs,
            redis_host="localhost",
        )

        assert len(results) == 3
        assert all(r.success for r in results.values())

        # Verify Redis calls
        mock_enqueue_and_poll.assert_called_once()
        mock_ci_results_converter.assert_called_once()


class TestExperimentFlowEmptyTrials:
    """Test handling of empty trial lists."""

    def test_empty_trial_results_no_post_trial_jobs(self) -> None:
        """Empty trial results creates no post-trial jobs."""
        jobs = create_post_trial_jobs([], {})
        assert jobs == []

    def test_empty_post_trial_jobs_empty_results(self) -> None:
        """Empty post-trial jobs returns empty results."""
        results = execute_post_trial_analysis([], redis_host="localhost")
        assert results == {}


class TestExperimentModuleExports:
    """Test that all required functions are exported."""

    def test_all_functions_importable(self) -> None:
        """All required functions can be imported from crsbench.experiment."""
        from crsbench.experiment import (
            CRSRunJob,
            TrialResult,
            create_post_trial_jobs,
            execute_post_trial_analysis,
        )

        # Verify they're callable/classes
        assert callable(create_post_trial_jobs)
        assert callable(execute_post_trial_analysis)
        assert isinstance(CRSRunJob, type)
        assert isinstance(TrialResult, type)


class TestJobTypesAndDependencies:
    """Test job types and dependency relationships."""

    def test_coverage_jobs_have_correct_type(self, tmp_path: Path) -> None:
        """FlatCollectCoverageJob has job_type 'verify'."""
        result = TrialResult(
            trial_id="trial-1",
            benchmark_path=tmp_path / "bench",
            harness_name="harness",
            trial_output_dir=tmp_path / "out",
            success=True,
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([result], {})
        assert len(jobs) == 1
        assert jobs[0].job_type == "verify"

    def test_crs_run_job_has_correct_type(self, tmp_path: Path) -> None:
        """CRSRunJob has job_type 'crs_run'."""
        job = CRSRunJob(
            crs_config_name="test-crs",
            benchmark_path=tmp_path / "bench",
            harness_name="harness",
            trial_num=1,
            trial_output_dir=tmp_path / "out",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=tmp_path / "registry",
            benchmarks_root=tmp_path / "benchmarks",
        )
        assert job.job_type == "crs_run"
